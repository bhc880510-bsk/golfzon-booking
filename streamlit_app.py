# 골프존 카운티 전체 예약 Streamlit 앱 (UI: 뉴서울CC 스타일 적용)
import warnings

# RuntimeWarning: coroutine '...' was never awaited 경고를 무시하도록 설정
warnings.filterwarnings(
    "ignore",
    message="coroutine '.*' was never awaited",
    category=RuntimeWarning
)

import streamlit as st
import datetime
import threading
import time
import queue
import sys
import traceback
import requests
import ujson as json
import urllib3
import re
import pytz
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup

# InsecureRequestWarning 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# KST 시간대 객체 전역 정의
KST = pytz.timezone('Asia/Seoul')

# ============================================================
# [수정] 골프존 카운티 골프장 목록 (golfclubSeq)
# 사용자가 이 목록을 쉽게 수정할 수 있도록 상단에 배치합니다.
# (출처: 골프존 국내골프장 번호.txt)
# ============================================================
GOLFZON_CLUB_MAP = {
    # (경기도)
    "이글몬트": "64",
    "안성H": "53",
    "안성W": "2",
    "송도": "68",
    # (충청)
    "진천": "4",
    "화랑": "52",
    # (경상)
    "감포cc": "1",
    "경남": "49",
    "사천": "56",
    "더골프": "61",
    "구미": "50",
    "청통": "58",
    "선산": "28",
    # (전라)
    "영암45": "59",
    "드래곤": "55",
    "순천": "57",
    "선운": "5",
    "무주": "54",
    # (제주)
    "제주오라": "3",
}
# ============================================================


# [수정] 앱 제목 변경
st.set_page_config(
    page_title="골프존 카운티 예약",  # "감포CC" -> "카운티"
    page_icon="⛳",
    layout="wide",  # 넓은 레이아웃 유지
)


# ============================================================
# Session State Initialization
# ============================================================
def get_default_date(days):
    """Gets a default date offset by 'days' from today (KST)."""
    return (datetime.datetime.now(KST).date() + datetime.timedelta(days=days))


# --- Utility Functions ---

def log_message(message, message_queue):
    """Logs a message with KST timestamp to the queue."""
    try:
        now_kst = datetime.datetime.now(KST)
        timestamp = now_kst.strftime('%H:%M:%S.%f')[:-3]
        message_queue.put(f"UI_LOG:[{timestamp}] {message}")
    except Exception:
        pass


def format_time_for_api(time_str):
    """Converts HH:MM to HHMM."""
    if not isinstance(time_str, str): time_str = str(time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{3,4}$', time_str) and time_str.isdigit():
        if len(time_str) == 4:
            return time_str
        elif len(time_str) == 3:
            return f"0{time_str}"
    return "0000"


def format_time_for_display(time_str):
    """Converts HHMM or HH:MM string to HH:MM display format."""
    if not isinstance(time_str, str): time_str = time_str.strftime('%H:%M') if isinstance(time_str,
                                                                                          datetime.time) else str(
        time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{4}$', time_str) and time_str.isdigit():
        return f"{time_str[:2]}:{time_str[2:]}"
    if len(time_str) == 5 and time_str[2] == ':':
        return time_str
    return time_str


def wait_until(target_dt_kst, stop_event, message_queue, log_prefix="프로그램 실행", log_countdown=False):
    """Waits precisely until the target KST datetime, with a countdown."""
    global KST

    now_kst = datetime.datetime.now(KST)
    remaining_seconds = (target_dt_kst - now_kst).total_seconds()
    log_remaining_start = 30

    log_message(f"⏳ {log_prefix} 대기중: {target_dt_kst.strftime('%H:%M:%S.%f')[:-3]} (KST 기준)", message_queue)

    if remaining_seconds <= 0.001:
        log_message(f"⚠️ 목표 시간이 이미 지났거나 도달했습니다. 즉시 실행.", message_queue)
        return

    if log_countdown and remaining_seconds > log_remaining_start:
        time_to_sleep_long = remaining_seconds - log_remaining_start
        log_message(
            f"⏳ {log_prefix} 대기중: {target_dt_kst.strftime('%H:%M:%S')}까지 {remaining_seconds:.1f}초 남음. ({log_remaining_start}초 전부터 카운트다운 시작)",
            message_queue
        )
        time.sleep(max(0, time_to_sleep_long))

        if stop_event.is_set():
            log_message("🛑 대기 중 중단 신호 수신.", message_queue)
            return

    if log_countdown:
        remaining_seconds = (target_dt_kst - datetime.datetime.now(KST)).total_seconds()
        countdown_start = int(remaining_seconds)

        for seconds_left in range(countdown_start, 0, -1):
            if stop_event.is_set():
                log_message("🛑 대기 중 중단 신호 수신.", message_queue)
                return

            log_message(f"⏳ 예약시도 대기중 : {seconds_left}초", message_queue)

            next_log_time = target_dt_kst - datetime.timedelta(seconds=(seconds_left - 1))
            sleep_duration = (next_log_time - datetime.datetime.now(KST)).total_seconds()

            if sleep_duration > 0:
                time.sleep(sleep_duration)
            else:
                time.sleep(0.01)

            if seconds_left == 1:
                break

    if not stop_event.is_set():
        final_wait = (target_dt_kst - datetime.datetime.now(KST)).total_seconds()

        if final_wait > 0:
            time.sleep(final_wait)

        actual_diff = (datetime.datetime.now(KST) - target_dt_kst).total_seconds()
        log_message(f"✅ 목표 시간 도달! {log_prefix} 스레드 즉시 실행. (종료 시각 차이: {actual_diff * 1000:.3f}ms)", message_queue)


# ============================================================
# API Booking Core Class (골프존 카운티 공용)
# ============================================================
class APIBookingCore:
    # [수정] __init__에 golfclub_seq 파라미터 추가
    def __init__(self, log_func, message_queue, stop_event, golfclub_seq):
        self.log_message_func = log_func
        self.message_queue = message_queue
        self.stop_event = stop_event
        self.session = requests.Session()
        self.member_id = None
        self.proxies = None
        self.KST = pytz.timezone('Asia/Seoul')

        # [수정] GAMPO_SEQ -> GOLFCLUB_SEQ 로 변경 (범용성)
        self.GOLFCLUB_SEQ = golfclub_seq

        # 핵심 URL 정의 (골프존 카운티 기준)
        self.API_DOMAIN = "https://www.golfzoncounty.com"
        self.LOGIN_URL = f"{self.API_DOMAIN}/login/userLogin"  #
        self.TIME_LIST_URL = f"{self.API_DOMAIN}/reserve/golfclub/teetime/getList"  #
        self.BOOK_CHECK_URL = f"{self.API_DOMAIN}/reserve/checkReserveTeetimeAble"  #
        # 최종 예약 URL (예상되는 일반적인 골프존 카운티 예약 최종 URL 사용)
        self.BOOK_SUBMIT_URL = f"{self.API_DOMAIN}/reserve/postReserveConfirmSubmit"

        # 코스 맵핑 (골프존 감포는 IN/OUT 18홀로 추정되지만, 코드에서는 IN/OUT 코스 코드가 A/B/C 등이 될 수 있어, 파싱 데이터 사용)
        self.course_detail_mapping = {
            "A": "OUT",
            "B": "IN",
            "C": "EAST",  # 다른 카운티 고려, 감포는 IN/OUT 위주
        }

    def log_message(self, msg):
        """Logs a message via the provided log function."""
        self.log_message_func(msg, self.message_queue)

    # ----------------------------------------------------
    # 기본 헤더 (골프존 카운티 기준)
    # ----------------------------------------------------
    def get_base_headers(self, referer_url=None):
        """
        기본 헤더를 반환하고, 세션에 저장된 모든 쿠키를 'Cookie' 헤더로 포함합니다.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Connection": "keep-alive",
            "Host": "www.golfzoncounty.com",
            "X-Requested-With": "XMLHttpRequest",
            # [최종 추가] POST 요청의 타입을 명시적으로 지정
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        # 세션에 저장된 쿠키를 문자열로 직렬화하여 'Cookie' 헤더에 추가
        if self.session and self.session.cookies:
            cookie_str = "; ".join([f"{name}={value}" for name, value in self.session.cookies.items()])
            if cookie_str:
                headers['Cookie'] = cookie_str

        return headers

    # 골프존 카운티 로그인 로직 (POST URL 직접 지정)
    def requests_login(self, usrid, usrpass):
        """
        골프존 카운티의 AJAX 기반 로그인(`userLogin`)을 수행합니다.
        POST 요청 URL을 "https://www.golfzoncounty.com/login/userLogin"로 명시합니다.
        """
        self.session = requests.Session()
        self.session.verify = False

        # [수정] 로그인 관련 URL을 명시적으로 재정의
        login_get_url = f"{self.API_DOMAIN}/login?gfsReturn=/setting/account"  # GET 요청 URL
        login_post_url = f"{self.API_DOMAIN}/login/userLogin"  # POST 요청 URL (로그에 명시됨)

        # ------------------------------------------------------------------
        # 1단계: 로그인 페이지 GET 요청 (세션 안정화 및 Hidden Field 확보)
        # ------------------------------------------------------------------
        hidden_fields = {}
        try:
            self.log_message("⏳ 로그인 POST 전, 로그인 페이지 GET 요청으로 숨겨진 필드 확보 시도...")
            get_headers = self.get_base_headers(login_get_url)
            get_headers["Content-Type"] = "text/html"

            res_get = self.session.get(login_get_url, headers=get_headers, timeout=5, verify=False)
            res_get.raise_for_status()

            # Hidden Field 파싱 (BeautifulSoup가 필요함)
            soup = BeautifulSoup(res_get.text, 'html.parser')
            for input_tag in soup.find_all('input', type='hidden'):
                name = input_tag.get('name')
                value = input_tag.get('value', '')
                if name:
                    hidden_fields[name] = value

            if not self.session.cookies.get('JSESSIONID'):
                self.log_message("⚠️ GET 요청 후 JSESSIONID 쿠키 확보 실패. 로그인 실패 가능성 있음.")
            else:
                self.log_message(f"✅ 로그인 페이지 GET 성공. 세션 쿠키 확보 완료.")

            if hidden_fields:
                self.log_message(f"✅ 숨겨진 필드 {list(hidden_fields.keys())} 확보 완료.")

        except requests.RequestException as e:
            self.log_message(f"❌ 로그인 페이지 GET 오류: {e}")
            return {'result': 'fail', 'message': 'Pre-login GET Network Error'}

        # ------------------------------------------------------------------
        # 2단계: 로그인 POST 요청 (POST URL 및 Referer 헤더 사용)
        # ------------------------------------------------------------------
        login_headers = self.get_base_headers(login_post_url)
        login_headers["Accept"] = "application/json, text/javascript, */*; q=0.01"

        # Referer를 GET 요청을 보낸 페이지 URL로 정확히 설정
        login_headers["Referer"] = login_get_url

        try:
            self.log_message("✅ 최종 Payload 생성 및 POST URL, Referer 헤더 수정 완료.")

            # 로그인 폼 데이터 (Payload) - Hidden fields + ID/PW
            login_data = {
                "userId": usrid,
                "userPw": usrpass,
            }
            login_data.update(hidden_fields)  # 파싱한 숨겨진 필드(토큰 등) 추가

            # 로그인 POST 요청 (login_post_url 사용)
            res = self.session.post(login_post_url, headers=login_headers, data=login_data, timeout=10,
                                    verify=False,
                                    allow_redirects=False)
            res.raise_for_status()  # 200 OK 확인

            # 3단계: 로그인 성공 확인 (JSON 응답 확인)
            try:
                login_response_json = res.json()

                # [핵심 수정] "resultCode" 대신 "result" 필드를 확인하고, 성공 코드를 숫자 0으로 간주
                result_code = login_response_json.get('result', None)
                fail_msg = login_response_json.get('message', '로그인 실패')

                # result가 0(숫자)이거나 '0'(문자열)일 때 성공으로 처리합니다.
                if result_code is not None and (result_code == 0 or str(result_code) == '0'):
                    self.log_message("🎉 로그인 POST 성공! (서버 응답 'result': 0 확인).")

                    # [추가] 로그인 성공 후 'personId'를 멤버 변수에 저장하여 추후 예약에 사용
                    user_info = login_response_json.get('data', {}).get('userInfo', {})
                    self.member_id = user_info.get('personId', usrid)

                    return {'result': 'success', 'message': 'Login successful'}
                else:
                    self.log_message(f"❌ 로그인 실패 (서버 메시지): {fail_msg}")
                    self.log_message(f"📜 서버 응답 텍스트 (추가 정보): {res.text[:200]}...")
                    self.log_message("UI_ERROR:로그인 실패: ID/PW가 유효하지 않거나 서버 오류.")
                    return {'result': 'fail', 'message': fail_msg}
            except json.JSONDecodeError:
                # [수정] JSON 디코딩 실패 시 전체 응답 텍스트 출력
                self.log_message(f"❌ 로그인 체크 실패: JSON 응답 디코딩 실패. 응답 텍스트: {res.text[:100]}...")
                self.log_message(f"📜 서버 응답 텍스트 (추가 정보): {res.text[:200]}...")
                self.log_message("UI_ERROR:로그인 실패: 예상치 못한 서버 응답.")
                return {'result': 'fail', 'message': 'JSON decode error'}

        except requests.RequestException as e:
            self.log_message(f"❌ 네트워크 오류: 로그인 실패: {e}")
            self.log_message("UI_ERROR:로그인 중 네트워크 오류 발생!")
            return {'result': 'fail', 'message': 'Network Error during login'}
        except Exception as e:
            self.log_message(f"❌ 로그인 처리 중 예기치 않은 오류 발생: {e}")
            return {'result': 'fail', 'message': f'Unexpected Error: {e}'}

    # 서버 시간 확인 URL
    def get_server_time_offset(self):
        """Fetches server time from HTTP Date header and calculates offset from local KST."""
        # [수정] 404 오류가 발생하던 /reserve 대신 /login 페이지를 사용하여 서버 시간 확인
        url = f"{self.API_DOMAIN}/login"
        max_retries = 5
        self.log_message("🔄 골프존 카운티 서버 시간 확인 시도...")
        for attempt in range(max_retries):
            try:
                # GET 요청으로 Date 헤더를 얻음
                response = self.session.get(url, timeout=5, verify=False)
                response.raise_for_status()
                server_date_str = response.headers.get("Date")

                if server_date_str:
                    server_time_gmt = parsedate_to_datetime(server_date_str)
                    server_time_kst = server_time_gmt.astimezone(KST)
                    local_time_kst = datetime.datetime.now(KST)
                    time_difference = (server_time_kst - local_time_kst).total_seconds()
                    self.log_message(
                        f"✅ 서버 시간 확인 성공: 서버 KST={server_time_kst.strftime('%H:%M:%S.%f')[:-3]}, 로컬 KST={local_time_kst.strftime('%H:%M:%S.%f')[:-3]}, Offset={time_difference:.3f}초")
                    return time_difference
                else:
                    self.log_message(f"⚠️ 서버 Date 헤더 없음, 재시도 ({attempt + 1}/{max_retries})...")
            except requests.RequestException as e:
                self.log_message(f"⚠️ 서버 시간 요청 실패: {e}, 재시도 ({attempt + 1}/{max_retries})...")
            except Exception as e:
                self.log_message(f"❌ 서버 시간 처리 중 오류: {e}")
                return 0
            time.sleep(0.5)

        self.log_message("❌ 서버 시간 확인 최종 실패. 시간 오차 보정 없이 진행합니다 (Offset=0).")
        return 0

    # 세션 유지 (선택된 CC 예약 메인 페이지)
    def keep_session_alive(self, target_dt):
        """Periodically hits a page to keep the session active until target_dt (1분에 1회)."""
        self.log_message("✅ 세션 유지 스레드 시작.")
        # [수정] GOLFCLUB_SEQ 사용
        keep_alive_url = f"{self.API_DOMAIN}/reserve/main/teetimeList?golfclubSeq={self.GOLFCLUB_SEQ}"
        interval_seconds = 60.0

        while not self.stop_event.is_set() and datetime.datetime.now(self.KST) < target_dt:
            try:
                headers = self.get_base_headers(keep_alive_url)
                headers["Content-Type"] = "application/json"
                self.session.get(keep_alive_url, headers=headers, timeout=10, verify=False, proxies=self.proxies)
                self.log_message("💚 [세션 유지] 세션 유지 요청 완료.")
            except Exception as e:
                self.log_message(f"❌ [세션 유지] 통신 오류 발생: {e}")

            start_wait = time.monotonic()
            while time.monotonic() - start_wait < interval_seconds:
                if self.stop_event.is_set() or datetime.datetime.now(self.KST) >= target_dt:
                    break
                time.sleep(1)

        if self.stop_event.is_set():
            self.log_message("🛑 세션 유지 스레드: 중단 신호 감지. 종료합니다.")
        else:
            self.log_message("✅ 세션 유지 스레드: 예약 정시 도달. 종료합니다.")

    # 'getList' 호출 (티타임 목록 HTML 획득)
    def get_all_available_times(self, date):
        """
        [수정] 사용자 관찰에 따라 pageNo 파라미터를 추가하고 1~4페이지를 모두 조회하여 HTML을 병합합니다.
        """
        self.log_message(f"⏳ {date} 선택된 골프장 예약 가능 시간대 조회 중 (HTML 요청 - getList, 최대 4페이지)...")

        url = self.TIME_LIST_URL
        # [수정] GOLFCLUB_SEQ 사용
        referer_url = f"{self.API_DOMAIN}/reserve/main/teetimeList?golfclubSeq={self.GOLFCLUB_SEQ}"
        headers = self.get_base_headers(referer_url)
        headers["Accept"] = "text/html, */*; q=0.01"

        all_times_html_parts = []
        max_pages = 4  # 사용자 관찰에 따라 1부터 4페이지까지 시도

        for page_no in range(1, max_pages + 1):
            if self.stop_event.is_set(): return None

            payload = {
                # [수정] GOLFCLUB_SEQ 사용
                "golfclubSeq": self.GOLFCLUB_SEQ,
                "selectDate": date,
                "selectTimeSection": "",
                "selectHoleCnt": "",
                "selectPersonCnt": "",
                #                "selectHoleCnt": "18",
                #                "selectPersonCnt": "4",
                "selectCaddieType": "",
                "selectReserveOrderType": "",
                "searchFlag": "Y",
                "searchTime": "",
                "pageNo": str(page_no)  # <--- [핵심 수정] pageNo 추가
            }

            max_attempts = 3
            timeout_seconds = 3.0

            for attempt in range(1, max_attempts + 1):
                if self.stop_event.is_set(): return None
                try:
                    self.log_message(f"🔄 티 타임 조회 시도 ({page_no}페이지, 시도 {attempt}/{max_attempts})...")
                    res = self.session.post(url, headers=headers, data=payload, timeout=timeout_seconds,
                                            verify=False)
                    res.raise_for_status()

                    if 'text/html' in res.headers.get('content-type', ''):
                        if len(res.text.strip()) < 100:
                            self.log_message(f"✅ 'getList' {page_no}페이지 응답 내용이 짧아 (목록 없음) 조회 종료.")
                        else:
                            self.log_message(f"✅ 'getList' {page_no}페이지 HTML 응답 수신 성공.")
                            all_times_html_parts.append(res.text)
                        break  # 성공했으니 다음 페이지로 이동
                    else:
                        self.log_message(f"❌ 'getList' {page_no}페이지 응답 유형 오류: {res.headers.get('content-type')}")
                        continue

                except (requests.Timeout, requests.RequestException) as e:
                    error_msg = f"❌ 티 타임 조회 통신 오류 ({type(e).__name__}): {e}"
                    if attempt < max_attempts:
                        self.log_message(f"{error_msg}, ... 즉시 재시도...")
                        continue
                    else:
                        self.log_message(f"❌ 최종 ({max_attempts}회) 시도 실패: {error_msg}")
                        return None
                except Exception as e:
                    self.log_message(f"❌ 'getList' {page_no}페이지 예외 오류: {e}")
                    return None

        if not all_times_html_parts:
            self.log_message("❌ 모든 페이지에서 티 타임 목록 조회 실패.")
            return None

        # 수집된 모든 HTML 조각을 하나로 합쳐서 반환
        combined_html = "".join(all_times_html_parts)
        self.log_message(f"✅ 총 {len(all_times_html_parts)}개 페이지 HTML 조합 완료. {len(combined_html)} 길이.")
        return combined_html

    # HTML 파싱 및 코스 필터링/정렬 로직
    def filter_and_sort_times(self, all_times_html, start_time_str, end_time_str, target_course_names, is_reverse):
        """
        HTML을 파싱하여 시간대와 코스를 필터링하고 정렬합니다.
        [수정] 코스 필터링 로직을 좀 더 범용적으로 수정 (IN/OUT 외에도 대응)
        """
        start_time_api = format_time_for_api(start_time_str)  # HHMM
        end_time_api = format_time_for_api(end_time_str)  # HHMM

        if not all_times_html:
            self.log_message("❌ 'getList'로부터 HTML 응답을 받지 못했습니다. 파싱 중단.")
            return []

        parsed_times = []
        try:
            soup = BeautifulSoup(all_times_html, 'html.parser')

            # 1. 예약 가능한 '<li>' 태그를 모두 찾습니다. (onclick="teetimeReserveConfirm(this)")
            available_list_items = soup.find_all('li', onclick=lambda h: h and 'teetimeReserveConfirm' in h)  #

            self.log_message(f"🔍 HTML 파싱: {len(available_list_items)}개의 예약 가능 시간 발견.")

            for li in available_list_items:
                try:
                    # 2. 핵심 정보 추출 (data-*)
                    bk_time_api = li.get('data-bookg-time')  # '1735'
                    time_table_id = li.get('data-time-table-id')  # '12094331'
                    course_cd_code = li.get('data-course-cd-code')  # 'B'

                    # 3. 코스 이름 추출 (IN/OUT)
                    course_span = li.find('div', class_='info').find('span')
                    course_nm = course_span.text.strip() if course_span else "알수없음"  # [수정] .strip() 추가

                    # 4. 시간 필터링 (UI 기준)
                    if start_time_api <= bk_time_api <= end_time_api:
                        # (bk_time, time_table_id, course_cd_code, course_nm)
                        parsed_times.append(
                            (bk_time_api, time_table_id, course_cd_code, course_nm)
                        )
                except Exception as e:
                    self.log_message(f"⚠️ HTML 리스트 아이템 1개 파싱 중 오류: {e}")

        except Exception as e:
            self.log_message(f"❌ HTML 파싱 중 치명적 오류: {e}")
            self.log_message("UI_ERROR:HTML 파싱 라이브러리(BeautifulSoup) 오류 발생.")
            return []

        # 5. 코스 필터링: target_course_names (ALL, IN, OUT)에 따라 필터링
        final_filtered_times = []

        # [수정] UI에서 'ALL'을 선택하면, 코스 이름(time_info[3])과 관계없이 모두 추가합니다.
        if target_course_names == "ALL":
            final_filtered_times = parsed_times
        else:
            # UI에서 IN 또는 OUT을 선택한 경우, 파싱된 코스 이름(time_info[3])과 일치하는 것만 필터링
            for time_info in parsed_times:
                # time_info[3] is course_nm (e.g., 'IN' or 'OUT')
                if time_info[3] == target_course_names:
                    final_filtered_times.append(time_info)

        # 6. 정렬
        # (bk_time, time_table_id, course_cd_code, course_nm)
        final_filtered_times.sort(key=lambda x: (x[0], x[2]), reverse=is_reverse)

        # 7. 상위 5개 로그 출력
        formatted_times = [f"{format_time_for_display(t[0])} ({t[3]})" for t in
                           final_filtered_times]  # t[3] = course_nm

        self.log_message(f"🔍 필터링/정렬 완료 (순서: {'역순' if is_reverse else '순차'}) - {len(final_filtered_times)}개 발견")
        if formatted_times:
            self.log_message("📜 **[최종 예약 우선순위 5개]**")
            for i, time_str in enumerate(formatted_times[:5]):
                self.log_message(f"   {i + 1}순위: {time_str}")
        else:
            self.log_message("ℹ️ **[알림]** 필터링 조건 (시간대/코스)에 맞는 예약 가능 시간이 없습니다.")

        return final_filtered_times

    # 예약 시도 로직 (2단계 - Check & Submit)
    def try_reservation(self, date, time_table_id, course_cd_code, time_api, course_name):
        """
        'checkReserveTeetimeAble' (1단계) 및 'postReserveConfirmSubmit' (2단계)를 순차적으로 시도합니다.
        """
        # format_time_for_display 함수는 정의되어 있다고 가정합니다.
        time_display = format_time_for_display(time_api)

        # ------------------------------------------------------------------
        # ⛔ 1단계: checkReserveTeetimeAble 호출 (예약 가능 여부 확인)
        # ------------------------------------------------------------------
        url_step1 = self.BOOK_CHECK_URL
        # [수정] GOLFCLUB_SEQ 사용
        referer_url_step1 = f"{self.API_DOMAIN}/reserve/main/teetimeList?golfclubSeq={self.GOLFCLUB_SEQ}"
        headers_step1 = self.get_base_headers(referer_url_step1)
        headers_step1["Accept"] = "application/json, text/javascript, */*; q=0.01"

        # GET 요청 파라미터
        params_step1 = {
            # [수정] GOLFCLUB_SEQ 사용
            "golfclubSeq": self.GOLFCLUB_SEQ,
            "accountId": self.member_id,
            "timeTableId": time_table_id,
            "reserveOrderType": "",
            "timeTableHasBookgInfoId": ""
        }

        try:
            res_step1 = self.session.get(url_step1, headers=headers_step1, params=params_step1,
                                         timeout=10, verify=False)
            res_step1.raise_for_status()

            if 'application/json' not in res_step1.headers.get('content-type', ''):
                self.log_message(f"❌ 1단계 오류: 서버 응답이 JSON이 아닙니다. HTML 응답 길이: {len(res_step1.text)}.")
                self.log_message(f"📜 응답 스니펫 (HTML/Text): {res_step1.text[:100]}...")
                return False, "1단계 오류: 예상치 못한 서버 응답 유형 (JSON 아님/세션 만료)"

            data_step1 = res_step1.json()

            # [수정된 성공 기준] 'result': 0 이고 'data.success': true 인지 확인
            api_result_code = data_step1.get('result')
            data_success = data_step1.get('data', {}).get('success')

            if api_result_code == 0 and data_success is True:
                self.log_message(f"✅ 1단계('checkReserveTeetimeAble') 성공: 예약 가능 확인됨 (Result: 0)")
            else:
                result_msg = data_step1.get('message', '1단계 응답 서버 메시지 없음')
                self.log_message(
                    f"❌ 1단계 실패 (Result Code: {api_result_code}, Data Success: {data_success}): {result_msg}")
                self.log_message(f"📜 1단계 응답 전체: {res_step1.text}")
                return False, f"1단계 확인 실패: 예상치 못한 서버 응답"

        except requests.RequestException as e:
            self.log_message(f"❌ 1단계('checkReserveTeetimeAble') 네트워크 오류: {e}")
            return False, f"1단계 네트워크 오류: {e}"
        except json.JSONDecodeError:
            self.log_message(f"❌ 1단계('checkReserveTeetimeAble') JSON 파싱 오류: {res_step1.text[:200]}")
            self.log_message(f"📜 JSON 파싱 실패 응답 전체: {res_step1.text}")
            return False, "1단계 JSON 파싱 오류"
        except Exception as e:
            self.log_message(f"❌ 1단계('checkReserveTeetimeAble') 중 예외 오류: {e}")
            return False, f"1단계 예외 오류: {e}"

        # ------------------------------------------------------------------
        # ⛔ 2단계: postReserveConfirmSubmit 호출 (최종 예약)
        # ------------------------------------------------------------------
        url_step2 = self.BOOK_SUBMIT_URL
        referer_url = f"{self.API_DOMAIN}/reserve/confirm"
        headers_step2 = self.get_base_headers(referer_url)

        headers_step2["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers_step2["Accept"] = "application/json, text/javascript, */*; q=0.01"

        # [AttributeError 해결] datetime.datetime.now() 사용
        now_kst = datetime.datetime.now(self.KST)

        # [✅ 최종 PayLoad] 오류 해결을 위해 'accountId'를 '1'로 고정
        payload_step2 = {
            # ----------------------------------------------
            # 🔑 예약 및 사용자 정보 (수정된 핵심 필드)
            "bookgDate": date,  # 예약 날짜
            "accountId": "1",  # <--- FIX: 하드코딩된 '1'로 오류 해결
            "timeTableId": time_table_id,
            "playPlayerCnt": "4",
            "caddieYn": "Y",
            "genderScd": "on",

            # 🔑 시간 스탬프 필드 (최종 예약 요청 시각)
            "eventLockTime": now_kst.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],  # 밀리초까지 포함
            "eventConfirmTime": now_kst.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "eventUserCheckTime": now_kst.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        }
        self.log_message(f"🔎 2단계 PayLoad 전송 직전 값: {payload_step2}")

        try:
            self.log_message(f"🚀 **[최종 시도]** {time_display} ({course_name}) 예약 요청 전송...")

            res_step2 = self.session.post(url_step2, headers=headers_step2, data=payload_step2,
                                          timeout=10, verify=False)
            res_step2.raise_for_status()

            data_step2 = res_step2.json()

            # -------------------------------------------------------------
            # ✅ [수정된 성공 판단 로직] - reserveCompleteInfo 객체 존재 여부로 판단
            # -------------------------------------------------------------
            api_result = data_step2.get('result')
            data_success = data_step2.get('data', {}).get('success')
            reserve_info = data_step2.get('data', {}).get('reserveCompleteInfo')

            # 'result': 0, 'data.success': true, 'reserveCompleteInfo' 객체 존재 시 최종 성공
            if api_result == 0 and data_success is True and reserve_info:
                bookg_id = reserve_info.get('bookgInfoId', 'N/A')
                bookg_no = reserve_info.get('bookgNo', 'N/A')

                self.log_message(f"🎉 **[대성공]** 최종 예약 완료! (시간: {time_display}, 코스: {course_name})")
                self.log_message(f"✅ 예약 ID: {bookg_id}, 예약 번호: {bookg_no}")

                return True, f"예약 성공 (예약번호: {bookg_no})"
            # -------------------------------------------------------------

            # 예약 실패 또는 예상치 못한 응답
            else:
                result_code = data_step2.get('resultCode')
                return_msg = data_step2.get('message', '서버 메시지 없음')

                limited_msg = return_msg.replace('\r', ' ').replace('\n', ' ')
                self.log_message(
                    f"❌ 2단계('postReserveConfirmSubmit') 실패 (Result Code: {result_code}/Result: {api_result}): {limited_msg}")
                self.log_message(f"📜 2단계 응답 전체: {res_step2.text}")
                return False, return_msg

        except requests.RequestException as e:
            self.log_message(f"❌ 2단계('postReserveConfirmSubmit') 네트워크 오류: {e}")
            return False, f"2단계 네트워크 오류: {e}"
        except json.JSONDecodeError:
            self.log_message(f"❌ 2단계('postReserveConfirmSubmit') JSON 파싱 오류: {res_step2.text[:200]}")
            self.log_message(f"📜 2단계 JSON 파싱 실패 응답 전체: {res_step2.text}")
            return False, "2단계 JSON 파싱 오류"
        except Exception as e:
            self.log_message(f"❌ 2단계('postReserveConfirmSubmit') 중 예외 오류: {e}")
            return False, f"2단계 예외 오류: {e}"

    def run_api_booking(self, inputs, sorted_available_times):
        """Attempts reservation on sorted times, up to top 5, with 3-retry logic."""
        if not sorted_available_times:
            self.log_message("ℹ️ 설정된 조건에 맞는 예약 가능 시간대가 없습니다. API 예약 중단.")
            return False

        target_date = inputs['target_date']
        test_mode = inputs.get('test_mode', True)

        if test_mode:
            # 튜플 구조: (bk_time, time_table_id, course_cd_code, course_nm)
            first_time_info = sorted_available_times[0]
            formatted_time = f"{format_time_for_display(first_time_info[0])} ({first_time_info[3]})"
            self.log_message(f"✅ 테스트 모드: 1순위 예약 가능 시간 확인: {formatted_time} (실제 예약 시도 안함)")
            return True

        self.log_message(f"🔎 정렬된 시간 순서대로 (상위 {min(5, len(sorted_available_times))}개) 예약 시도...")

        # Try booking the top 5
        for i, time_info in enumerate(sorted_available_times[:5]):
            if self.stop_event.is_set():
                self.log_message("🛑 예약 시도 중 중단됨.")
                break

            # 튜플 구조: (bk_time, time_table_id, course_cd_code, course_nm)
            bk_time_api = time_info[0]
            time_table_id = time_info[1]
            course_cd_code = time_info[2]
            course_name = time_info[3]
            time_display = format_time_for_display(bk_time_api)

            # 3회 재시도 루프
            for attempt in range(1, 4):
                if self.stop_event.is_set():
                    self.log_message("🛑 예약 시도 중 중단됨.")
                    return False

                self.log_message(f"⭐ {i + 1}순위({time_display}, {course_name}) 예약 시도 ({attempt}/3회)...")

                success, message = self.try_reservation(
                    date=target_date,
                    time_table_id=time_table_id,
                    course_cd_code=course_cd_code,
                    time_api=bk_time_api,
                    course_name=course_name
                )

                if success:
                    # 최종 성공 시 전체 루프 중단
                    return True
                else:
                    self.log_message(f"❌ 예약 시도 실패: {message}")
                    if "이미 예약되어 있습니다" in message or "마감되었습니다" in message:
                        self.log_message("❌ [경고] 이미 예약된 타임 또는 마감. 다른 시간대로 이동합니다.")
                        break
                    elif attempt < 3:
                        self.log_message("🔄 3초 후 재시도...")
                        time.sleep(3)

            if not success and not self.stop_event.is_set():
                self.log_message(f"❗ {i + 1}순위({time_display}) 3회 모두 최종 실패. 다음 시간대로 이동.")

        if not self.stop_event.is_set():
            self.log_message(f"❌ 상위 {min(5, len(sorted_available_times))}개 시간대 예약 시도 최종 실패.")
            return False


# ============================================================
# Main Threading Logic - start_pre_process
# ============================================================
def start_pre_process(message_queue, stop_event, inputs):
    """Main background thread function orchestrating the booking process."""
    global KST
    # 📌 1. 안전 마진 설정 (0.200초)
    SAFETY_MARGIN_SECONDS = 0.200
    log_message("[INFO] ⚙️ 예약 시작 조건 확인 완료.", message_queue)
    try:
        # [수정] APIBookingCore 생성 시 inputs['golfclub_seq'] 전달
        core = APIBookingCore(
            log_message,
            message_queue,
            stop_event,
            inputs['golfclub_seq']
        )

        # 1. Login
        log_message("🔒 로그인 시도...", message_queue)
        login_result = core.requests_login(inputs['id'], inputs['password'])
        if login_result['result'] != 'success':
            log_message(f"❌ 로그인 실패: {login_result['message']}", message_queue)
            return
        log_message("✅ 로그인 성공.", message_queue)
        log_message("⏳ 로그인 성공. 세션 활성화 전 2초간 대기 (에러 방지)...", message_queue)
        time.sleep(2.0)
        if stop_event.is_set(): return

        # 2. Server Time Check & Target Time Calculation (Initial Offset)
        time_offset = core.get_server_time_offset()

        # [수정] run_date는 UI에서 입력받은 run_date_input을 사용합니다.
        # run_date와 run_time을 결합하여 KST datetime 객체를 생성합니다.
        run_date_str = inputs['run_date']  # YYYYMMDD
        run_time_str = inputs['run_time']  # HH:MM:SS
        target_dt_naive = datetime.datetime.strptime(f"{run_date_str}{run_time_str}", '%Y%m%d%H:%M:%S')
        target_dt_kst = KST.localize(target_dt_naive)

        target_local_time_kst = target_dt_kst - datetime.timedelta(seconds=time_offset)
        time.sleep(0.2)
        log_message(
            f"✅ [초기 목표 시간] Local KST 기준: {target_local_time_kst.strftime('%H:%M:%S.%f')[:-3]} (Offset: {time_offset:.3f}초 반영)",
            message_queue)
        if stop_event.is_set(): return

        # 3. FIX: Initial Reservation Page Access for Session
        log_message(f"🔎 **[선행 작업]** 예약 페이지 초기 진입 (세션 활성화)...", message_queue)
        # [수정] GOLFCLUB_SEQ를 core에서 참조하도록 변경
        try:
            core.session.get(f"{core.API_DOMAIN}/reserve/main/teetimeList?golfclubSeq={core.GOLFCLUB_SEQ}", timeout=5.0,
                             verify=False)
            log_message("✅ 예약 페이지 초기 진입 완료. 세션 활성화.", message_queue)
        except requests.RequestException as e:
            log_message(f"❌ 예약 페이지 초기 진입 실패: {e}", message_queue)
            log_message("UI_ERROR:예약 페이지(세션) 초기화 실패로 예약 프로세스 중단.", message_queue)
            return
        if stop_event.is_set(): return

        # 4. Session Keep-Alive Thread Start
        keep_alive_dt = target_local_time_kst - datetime.timedelta(seconds=5)
        keep_alive_thread = threading.Thread(
            target=core.keep_session_alive,
            args=(keep_alive_dt,),
            daemon=True
        )
        keep_alive_thread.start()
        log_message("✅ 세션 유지 스레드 시작 완료 (최종 예약 5초 전까지 유지).", message_queue)

        # 5. Wait for Final Offset Check Point (30 seconds before target time)
        countdown_start_time = target_dt_kst - datetime.timedelta(seconds=30)
        now_kst = datetime.datetime.now(KST)

        if now_kst < countdown_start_time:
            wait_until(countdown_start_time, stop_event, message_queue, "최종 시간 보정 대기", log_countdown=False)
            if stop_event.is_set(): return

            log_message("🔄 최종 예약 30초 전: 서버 시간 오차 재측정 및 보정 (부하 최소화 시점)", message_queue)
            final_time_offset = core.get_server_time_offset()

            target_local_time_kst = target_dt_kst - datetime.timedelta(seconds=final_time_offset)
            log_message(
                f"✅ 최종 목표 시간 재확정 (Local KST): {target_local_time_kst.strftime('%H:%M:%S.%f')[:-3]} (최종 Offset: {final_time_offset:.3f}초 반영)",
                message_queue)
        else:
            log_message("⚠️ [시간 경과] 이미 최종 예약 30초 전 시점을 지났습니다. 초기 오프셋으로 즉시 실행합니다.", message_queue)
            if stop_event.is_set(): return

        # 6. Wait until the Final Target Time (with Countdown)
        wait_until(target_local_time_kst, stop_event, message_queue, "최종 예약 시도", log_countdown=True)
        if stop_event.is_set(): return

        # 7. Apply Booking Delay (예약 지연)
        booking_delay = inputs.get('booking_delay', 0.0)
        try:
            if booking_delay > 0.001:
                log_message(f"⏳ 예약 지연 {booking_delay:.3f}초 적용...", message_queue)
                time.sleep(booking_delay)
        except Exception as e:
            log_message(f"❌ 예약 지연 적용 중 오류: {e}", message_queue)

        if stop_event.is_set(): return

        # 8. Get Available Times (getList API Call)
        log_message(
            f"🔎 🚀 **[골든 타임]** 티 타임 조회 시작 (HTML 요청)...",
            message_queue)
        log_message(
            f"🔎 필터링 조건: {inputs['start_time']}~{inputs['end_time']}, 코스: {inputs['course_type']}, 순서: {inputs['order']}",
            message_queue)

        all_times_html = core.get_all_available_times(inputs['target_date'])
        if not all_times_html:
            log_message("❌ 티 타임 목록 조회 실패. 예약 프로세스 중단.", message_queue)
            return
        if stop_event.is_set(): return

        # 9. Filter and Sort Times
        is_reverse = inputs['order'] == '역순 (늦은 시간 순)'
        target_course = inputs['course_type']

        sorted_available_times = core.filter_and_sort_times(
            all_times_html=all_times_html,
            start_time_str=inputs['start_time'],
            end_time_str=inputs['end_time'],
            target_course_names=target_course,
            is_reverse=is_reverse
        )
        if stop_event.is_set(): return

        # 10. Run API Booking attempts
        core.run_api_booking(inputs, sorted_available_times)

    except KeyError as e:
        log_message(f"[UI ALERT] 🛑 예상치 못한 오류 발생: KeyError - {e}", message_queue)
        log_message(f"디버깅 정보: Traceback: {traceback.format_exc()}", message_queue)

    except Exception as e:
        log_message(f"[UI ALERT] 🛑 예상치 못한 치명적인 오류 발생: {e}", message_queue)
        log_message(f"디버깅 정보: Traceback: {traceback.format_exc()}", message_queue)

    finally:
        log_message("[INFO] Worker 스레드 종료.", message_queue)


# ============================================================
# Streamlit UI & Thread Management
# ============================================================

# --- State Initialization ---
if 'log_messages' not in st.session_state:
    st.session_state.log_messages = ["프로그램 실행 준비 완료."]
if 'is_running' not in st.session_state:
    st.session_state.is_running = False
if 'stop_event' not in st.session_state:
    st.session_state.stop_event = threading.Event()
if 'worker_thread' not in st.session_state:
    st.session_state.worker_thread = None
if 'message_queue' not in st.session_state:
    st.session_state.message_queue = queue.Queue()
if 'log_container_placeholder' not in st.session_state:
    st.session_state.log_container_placeholder = None

# 초기값 설정
if 'target_date' not in st.session_state:
    st.session_state.target_date = get_default_date(30)
# [추가] 프로그램 실행일 초기값 설정 (오늘)
if 'run_date_input' not in st.session_state:
    st.session_state.run_date_input = get_default_date(0)
if 'run_time' not in st.session_state:
    st.session_state.run_time = datetime.time(9, 0, 0)
if 'start_time' not in st.session_state:
    st.session_state.start_time = datetime.time(6, 0)
if 'end_time' not in st.session_state:
    st.session_state.end_time = datetime.time(20, 0)
if 'order' not in st.session_state:
    st.session_state.order = '순차 (빠른 시간 순)'
if 'test_mode' not in st.session_state:
    st.session_state.test_mode = True
if 'booking_delay' not in st.session_state:
    st.session_state.booking_delay = 0.000
if 'id' not in st.session_state:
    st.session_state.id = ""
if 'password' not in st.session_state:
    st.session_state.password = ""
if 'course_type' not in st.session_state:
    st.session_state.course_type = 'ALL'

# [수정] 골프장 선택 상태 초기화
if 'selected_club_name' not in st.session_state:
    st.session_state.selected_club_name = list(GOLFZON_CLUB_MAP.keys())[0]  # 첫 번째 골프장을 기본값으로


# --- Helper Functions ---
def update_log_display():
    """Reads messages from the queue and updates the log display."""
    while not st.session_state.message_queue.empty():
        msg = st.session_state.message_queue.get_nowait()
        if msg.startswith("UI_LOG:"):
            st.session_state.log_messages.append(msg[7:])
        elif msg.startswith("UI_ERROR:"):
            st.session_state.log_messages.append(f"[UI ALERT] {msg[9:]}")


def stop_booking():
    """Sets the stop event and updates UI state."""
    if st.session_state.is_running:
        st.session_state.stop_event.set()
        st.session_state.is_running = False
        log_message("🛑 사용자 요청으로 프로그램을 중단합니다.", st.session_state.message_queue)


def run_booking():
    """Gathers inputs and starts the worker thread."""
    # 유효성 검사 로직 삭제 요청에 따라, ID/PW가 비어있는 경우에만 경고 메시지를 출력하고 리턴
    if not st.session_state.id or not st.session_state.password:
        log_message("[UI ALERT] ❌ ID와 비밀번호를 모두 입력해야 합니다.", st.session_state.message_queue)
        return

    # [수정] 선택된 골프장 이름으로 golfclub_seq 찾기
    selected_club_name = st.session_state.selected_club_name
    selected_golfclub_seq = GOLFZON_CLUB_MAP.get(selected_club_name)

    if not selected_golfclub_seq:
        log_message(f"[UI ALERT] ❌ 골프장 '{selected_club_name}'의 고유번호(seq)를 찾을 수 없습니다.", st.session_state.message_queue)
        return

    st.session_state.is_running = True
    st.session_state.stop_event.clear()

    # 입력값 정리
    inputs = {
        "id": st.session_state.id,
        "password": st.session_state.password,
        "target_date": st.session_state.target_date.strftime('%Y%m%d'),
        # [수정] run_date를 UI 입력값 run_date_input을 사용하도록 변경
        "run_date": st.session_state.run_date_input.strftime('%Y%m%d'),
        "run_time": st.session_state.run_time.strftime('%H:%M:%S'),
        "start_time": st.session_state.start_time.strftime('%H:%M'),
        "end_time": st.session_state.end_time.strftime('%H:%M'),
        "order": st.session_state.order,
        "test_mode": st.session_state.test_mode,
        "booking_delay": st.session_state.booking_delay,
        "course_type": st.session_state.course_type,

        # [수정] 선택된 골프장 고유번호(seq) 추가
        "golfclub_seq": selected_golfclub_seq,
        "golfclub_name": selected_club_name
    }

    # 로그 초기화
    st.session_state.log_messages = []
    log_message(f"💚 **[Worker 시작]** (Run ID: {datetime.datetime.now(KST).strftime('%Y%m%d%H%M%S')}) 💚",
                st.session_state.message_queue)
    log_message(f"⛳ **[Target]** {inputs['golfclub_name']} (Seq: {inputs['golfclub_seq']})",
                st.session_state.message_queue)

    # Worker Thread 시작
    st.session_state.worker_thread = threading.Thread(
        target=start_pre_process,
        args=(st.session_state.message_queue, st.session_state.stop_event, inputs),
        daemon=True
    )
    st.session_state.worker_thread.start()


# ============================================================
# Streamlit UI Definition
# ============================================================

# Custom CSS for better aesthetics and Title Styling
st.markdown("""
<style>
/* 1. 타이틀 스타일 수정 */
.main-title-container {
    text-align: center; /* 가운데 정렬 */
    margin-bottom: 20px;
}
.main-title {
    font-size: 26px !important; /* 글자 크기 26px로 축소 */
    font-weight: bold;
    color: #333333; /* 제목 색상 유지 */
}

/* 2. 섹션 헤더 스타일 */
.section-header {
    font-size: 18px;
    font-weight: bold;
    color: #007bff;
    margin-top: 10px;
    margin-bottom: 10px;
}
.stForm {
    padding: 10px;
    border: 1px solid #ccc;
    border-radius: 5px;
}
/* 3. Streamlit 기본 title 숨기기 */
.stApp header {
    visibility: hidden;
    height: 0px !important;
}
</style>
""", unsafe_allow_html=True)

# [수정] st.title 대신 markdown을 사용하여 제목을 중앙 정렬하고 크기를 조정 (감포CC -> 골프존 카운티)
st.markdown('<div class="main-title-container"><h1 class="main-title">⛳ 골프존 국내골프장 예약</h1></div>', unsafe_allow_html=True)

# --- 1. 로그인 정보 ---
st.markdown('<p class="section-header">🔑 로그인 정보</p>', unsafe_allow_html=True)

st.text_input("아이디 (ID)", key="id")
st.text_input("비밀번호 (Password)", type="password", key="password")

# --- 2. 예약 조건 설정 (메인 섹션) ---
st.markdown('<p class="section-header">⚙️ 예약 조건 설정</p>', unsafe_allow_html=True)

# [수정] 골프장 선택 UI 추가 (가장 위로)
st.selectbox(
    "⛳ 예약할 골프장",
    options=list(GOLFZON_CLUB_MAP.keys()),
    key="selected_club_name",
    help="예약할 골프장을 선택합니다. (목록은 코드 상단 GOLFZON_CLUB_MAP에서 수정)"
)

# [수정] 레이아웃을 3개 컬럼으로 재조정
col_reserve_date, col_run_date, col_run_time = st.columns(3)

with col_reserve_date:
    st.date_input(
        "📅 예약 목표 날짜",
        min_value=get_default_date(1),
        max_value=get_default_date(31),  # 골프존은 4주 후까지 가능하므로, 31일 설정
        key="target_date",
        help="예약을 시도할 날짜를 선택합니다."
    )

with col_run_date:
    # [복구] 프로그램 실행일 항목
    st.date_input(
        "📅 프로그램 실행일",
        min_value=get_default_date(0),
        max_value=get_default_date(31),
        key="run_date_input",  # 새로운 키 사용
        help="프로그램이 실제로 예약 시도를 시작할 날짜입니다. (일반적으로 '오늘')"
    )

with col_run_time:
    st.time_input(
        "⏰ 프로그램 실행 시간 (KST)",
        step=60,  # 1분 단위
        key="run_time",
        help="프로그램이 티 타임 조회/예약 시도를 시작할 시간을 설정합니다. (예: 09:00:00)"
    )

# [수정] 지연 시간과 테스트 모드를 2번째 줄에 배치
col_delay, col_mode, col_spacer = st.columns([1.5, 1, 0.5])

with col_delay:
    st.number_input(
        "⏱️ 예약 시도 지연 (초)",
        min_value=0.000,
        max_value=1.000,
        step=0.001,
        format="%.3f",
        key="booking_delay",
        help="티 타임 조회 후, 최종 예약 요청 전의 지연 시간(밀리초)입니다. 0.001초 단위로 조정 가능."
    )

with col_mode:
    st.markdown("<div style='height: 1.6rem;'></div>", unsafe_allow_html=True)  # 토글 정렬용
    st.toggle(
        "🧪 테스트 모드",
        key="test_mode",
        help="ON: 실제 예약 요청 없이 1순위 타임만 확인 후 종료합니다. OFF: 실제 최종 예약 시도."
    )
# col_spacer는 빈 공간

# --- 시간 필터링 및 코스/순서 설정 (3번째 줄) ---
col_start, col_end, col_course, col_order = st.columns([1, 1, 1.5, 1.5])

with col_start:
    st.time_input("시작 시간", key="start_time", step=1800, help="원하는 티 타임의 시작 시각.")

with col_end:
    st.time_input("종료 시간", key="end_time", step=1800, help="원하는 티 타임의 종료 시각.")

with col_course:
    course_options = ["ALL", "IN", "OUT"]
    st.selectbox(
        "선호 코스 선택",
        options=course_options,
        index=course_options.index(st.session_state.course_type),
        key="course_type",
        help="예약을 시도할 코스(ALL: 전체, IN/OUT: 특정 코스)를 선택합니다."
    )

with col_order:
    order_options = ['순차 (빠른 시간 순)', '역순 (늦은 시간 순)']
    st.selectbox(
        "티 타임 정렬 순서",
        options=order_options,
        index=order_options.index(st.session_state.order),
        key="order",
        help="필터링된 시간대 중 예약 시도 우선순위를 결정합니다."
    )

# --- 3. 실행 버튼 ---
st.markdown("---")
col_start, col_stop = st.columns([1, 1])

with col_start:
    st.button(
        "🚀 예약 시작",
        on_click=run_booking,
        disabled=st.session_state.is_running,
        type="primary",
        help="ID와 비밀번호를 입력하면 버튼이 활성화됩니다."
    )
with col_stop:
    st.button("❌ 취소", on_click=stop_booking, disabled=not st.session_state.is_running, type="secondary")

# --- 4. Log Section ---
st.markdown("---")  # Separator
st.markdown('<p class="section-header">📝 실행 로그</p>', unsafe_allow_html=True)

if st.session_state.log_container_placeholder is None:
    st.session_state.log_container_placeholder = st.empty()

# Log Display Logic
with st.session_state.log_container_placeholder.container(height=300):
    # Log Queue에서 메시지 가져와서 상태에 추가
    update_log_display()

    # Log Display (기존 골프존감포의 로그 색상 로직 유지)
    for msg in reversed(st.session_state.log_messages[-500:]):
        safe_msg = msg.replace("<", "&lt;").replace(">", "&gt;")
        color = "black"
        if "[UI ALERT]" in msg or "❌" in msg or "UI_ERROR" in msg:
            color = "red"
        elif "🎉" in msg or "✅" in msg and "대기중" not in msg:
            color = "green"
        elif "💚 [세션 유지]" in msg or "📜" in msg or "⛳ **[Target]**" in msg:
            color = "#007bff"
        elif "⏳" in msg or "🔄" in msg:
            color = "gray"

        st.markdown(f'<div style="color: {color}; font-size: 12px; font-family: monospace;">{safe_msg}</div>',
                    unsafe_allow_html=True)

# ------------------------------------------------------------
# 5. 실시간 업데이트
# ------------------------------------------------------------
# Streamlit Rerun (for real-time log updates)
if st.session_state.is_running:
    # Worker Thread가 종료되었는지 확인
    if st.session_state.worker_thread and not st.session_state.worker_thread.is_alive():
        st.session_state.is_running = False
        st.rerun()  # Worker 종료 후 UI 상태 업데이트
    else:
        time.sleep(0.1)
        st.rerun()  # 로그 업데이트를 위해 0.1초마다 재실행