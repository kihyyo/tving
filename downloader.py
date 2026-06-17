from __future__ import annotations

import math
import os
import re
import shutil
import time
import logging
import platform
import pathlib
import threading
import traceback
import hashlib
import subprocess
import struct
from base64 import b64encode
from copy import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from typing import Optional, Callable, List, Tuple, Dict
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree

from .setup import P

logger = P.logger or logging.getLogger(__name__)

SYSTEM = platform.system().lower()


def extract_drm_config(contents_json: dict) -> dict:

    play_info = contents_json.get('play_info') or {}

    # 신형 판별: license_url + header_key/header_value 키 존재 여부
    if 'license_url' in play_info and 'header_key' in play_info:
        return {
            'mpd_url':         contents_json.get('url') or play_info.get('uri', ''),
            'license_url':     play_info['license_url'],
            'license_headers': {
                'header_key':   play_info['header_key'],
                'header_value': play_info['header_value'],
            },
            'mpd_headers':     play_info.get('mpd_headers') or {},
        }

    # 구형: drm_license_uri / drm_key_request_properties
    return {
        'mpd_url':         contents_json.get('url') or play_info.get('uri', ''),
        'license_url':     play_info.get('drm_license_uri'),
        'license_headers': play_info.get('drm_key_request_properties') or {},
        'mpd_headers':     play_info.get('mpd_headers') or {},
    }


def _parse_boxes(data: bytes):
    """bytes 에서 (type, content) 박스 목록을 반환."""
    i = 0
    while i + 8 <= len(data):
        size = struct.unpack('>I', data[i:i+4])[0]
        box_type = data[i+4:i+8].decode('latin-1')
        if size == 0:
            payload = data[i+8:]
            yield box_type, payload
            break
        elif size == 1:
            if i + 16 > len(data):
                break
            size = struct.unpack('>Q', data[i+8:i+16])[0]
            payload = data[i+16:i+size]
            i += size
        else:
            payload = data[i+8:i+size]
            i += size
        yield box_type, payload


def _extract_kid_from_init(init_data: bytes) -> Optional[UUID]:
    """init segment 에서 Widevine PSSH 의 KID 를 추출."""
    try:
        for box_type, payload in _parse_boxes(init_data):
            if box_type == 'moov':
                for b2, p2 in _parse_boxes(payload):
                    if b2 == 'pssh':
                        # version(1) + flags(3) + system_id(16) + ...
                        if len(p2) < 20:
                            continue
                        system_id = UUID(bytes=p2[4:20])
                        WIDEVINE_UUID = UUID('edef8ba9-79d6-4ace-a3c8-27dcd51d21ed')
                        if system_id == WIDEVINE_UUID:
                            # version 1 PSSH has KIDs
                            version = p2[0]
                            if version == 1 and len(p2) >= 24:
                                kid_count = struct.unpack('>I', p2[20:24])[0]
                                if kid_count > 0 and len(p2) >= 24 + 16:
                                    return UUID(bytes=p2[24:40])
    except Exception:
        pass
    return None

NS_MAP = {
    'mpd': 'urn:mpeg:dash:schema:mpd:2011',
    'mspr': 'urn:microsoft:playready',
}

WIDEVINE_SCHEME  = 'urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed'
PLAYREADY_SCHEME = 'urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95'


def _pt_to_sec(d: str) -> float:
    if not d:
        return 0.0
    has_ymd = d[0:8] == 'P0Y0M0DT'
    if d[0:2] != 'PT' and not has_ymd:
        return 0.0
    d = d[6:].upper() if has_ymd else d[2:].upper()
    m = re.findall(r'([\d.]+.)', d)
    return sum(float(x[:-1]) * {'H': 3600, 'M': 60, 'S': 1}[x[-1].upper()] for x in m)


def _replace_fields(url: str, **kwargs) -> str:
    for field, value in kwargs.items():
        url = url.replace(f'${field}$', str(value))
        m = re.search(rf'\${re.escape(field)}%([a-z0-9]+)\$', url, flags=re.I)
        if m:
            url = url.replace(m.group(), f'{value:{m.group(1)}}')
    return url


def _get_attr(elem, attr, *fallbacks):
    """elem 에서 attr 를 찾고, 없으면 fallbacks 엘리먼트들 순서로 탐색."""
    v = elem.get(attr)
    if v is not None:
        return v
    for fb in fallbacks:
        if fb is None:
            continue
        v = fb.get(attr)
        if v is not None:
            return v
    return None


def _has_drm(representation, adaptation_set) -> bool:
    """ContentProtection 요소 유무로 DRM 여부 판단."""
    for elem in (representation, adaptation_set):
        if elem is None:
            continue
        for cp in elem.findall('ContentProtection'):
            scheme = (cp.get('schemeIdUri') or '').lower()
            if scheme in (WIDEVINE_SCHEME, PLAYREADY_SCHEME):
                return True
    return False


def _get_widevine_pssh(representation, adaptation_set) -> Optional[str]:
    """Widevine ContentProtection 에서 PSSH base64 추출."""
    for elem in (representation, adaptation_set):
        if elem is None:
            continue
        for cp in elem.findall('ContentProtection'):
            scheme = (cp.get('schemeIdUri') or '').lower()
            if scheme == WIDEVINE_SCHEME:
                pssh_elem = cp.find('pssh')
                if pssh_elem is not None and pssh_elem.text:
                    return pssh_elem.text.strip()
    return None


class TrackInfo:
    """파싱된 단일 트랙 정보."""
    __slots__ = (
        'content_type', 'rep_id', 'bandwidth', 'width', 'height',
        'codecs', 'language', 'segments', 'init_url', 'init_range',
        'is_drm', 'pssh_b64', 'timescale',
    )

    def __init__(self):
        self.content_type = ''
        self.rep_id = ''
        self.bandwidth = 0
        self.width = 0
        self.height = 0
        self.codecs = ''
        self.language = 'und'
        self.segments: List[Tuple[str, Optional[str]]] = []  # (url, range_or_None)
        self.init_url: Optional[str] = None
        self.init_range: Optional[str] = None  # "bytes=0-123"
        self.is_drm = False
        self.pssh_b64: Optional[str] = None
        self.timescale = 1


def _is_ad_period(period, mpd_has_drm: bool) -> bool:
    """
    광고/로고 Period 판별.
    - MPD 전체에 DRM이 있는데 이 Period에 ContentProtection이 없으면 광고/로고.
    - SupplementalProperty 에 PRE_ROLL/MID_ROLL/POST_ROLL 이 있으면 광고.
    """
    for prop in period.findall('.//SupplementalProperty'):
        if prop.get('value') in ('PRE_ROLL', 'MID_ROLL', 'POST_ROLL'):
            return True
    if mpd_has_drm:
        period_has_drm = bool(period.findall('.//ContentProtection'))
        if not period_has_drm:
            return True
    return False


def _parse_period_tracks(period, period_duration: float,
                          manifest_base: str, mpd_url: str) -> List[TrackInfo]:
    """단일 Period 에서 TrackInfo 목록 파싱."""
    tracks: List[TrackInfo] = []

    period_base = urljoin(manifest_base, period.findtext('BaseURL') or '')

    for adaptation_set in period.findall('AdaptationSet'):
        as_base = urljoin(period_base, adaptation_set.findtext('BaseURL') or '')

        for rep in adaptation_set.findall('Representation'):
            rep_base = urljoin(as_base, rep.findtext('BaseURL') or '')

            content_type = (_get_attr(rep, 'contentType', adaptation_set)
                            or (_get_attr(rep, 'mimeType', adaptation_set) or '').split('/')[0])
            if content_type not in ('video', 'audio', 'text'):
                continue

            t = TrackInfo()
            t.content_type = content_type
            t.rep_id = rep.get('id', '')
            t.bandwidth = int(rep.get('bandwidth') or 0)
            t.width = int(rep.get('width') or 0)
            t.height = int(rep.get('height') or 0)
            t.codecs = _get_attr(rep, 'codecs', adaptation_set) or ''
            t.language = (_get_attr(rep, 'lang', adaptation_set) or 'und').split('-')[0]
            t.is_drm = _has_drm(rep, adaptation_set)
            t.pssh_b64 = _get_widevine_pssh(rep, adaptation_set)

            seg_tmpl = rep.find('SegmentTemplate') or adaptation_set.find('SegmentTemplate')
            seg_list = rep.find('SegmentList') or adaptation_set.find('SegmentList')
            seg_base = rep.find('SegmentBase') or adaptation_set.find('SegmentBase')

            if seg_tmpl is not None:
                seg_tmpl = copy(seg_tmpl)
                t.timescale = float(seg_tmpl.get('timescale') or 1)
                start_num = int(seg_tmpl.get('startNumber') or 1)
                end_num   = int(seg_tmpl.get('endNumber') or 0) or None

                for attr in ('initialization', 'media'):
                    val = seg_tmpl.get(attr)
                    if val and not re.match(r'^https?://', val, re.I):
                        seg_tmpl.set(attr, urljoin(rep_base or mpd_url, val))
                    if val:
                        val2 = seg_tmpl.get(attr)
                        if not urlparse(val2).query:
                            mpd_q = urlparse(mpd_url).query
                            if mpd_q:
                                seg_tmpl.set(attr, f'{val2}?{mpd_q}')

                init_tpl = seg_tmpl.get('initialization')
                if init_tpl:
                    t.init_url = _replace_fields(init_tpl,
                                                  Bandwidth=rep.get('bandwidth'),
                                                  RepresentationID=rep.get('id'))

                seg_timeline = seg_tmpl.find('SegmentTimeline')
                if seg_timeline is not None:
                    cur = 0
                    durations = []
                    for s in seg_timeline.findall('S'):
                        if s.get('t'):
                            cur = int(s.get('t'))
                        for _ in range(1 + int(s.get('r') or 0)):
                            durations.append(cur)
                            cur += int(s.get('d'))
                    if not end_num:
                        end_num = len(durations)
                    for t_val, n in zip(durations, range(start_num, end_num + 1)):
                        url = _replace_fields(seg_tmpl.get('media'),
                                               Bandwidth=rep.get('bandwidth'),
                                               Number=n,
                                               RepresentationID=rep.get('id'),
                                               Time=t_val)
                        t.segments.append((url, None))
                else:
                    if not period_duration:
                        logger.warning('[downloader] SegmentTimeline 없고 duration 도 없음')
                        continue
                    seg_dur = float(seg_tmpl.get('duration') or 1)
                    if not end_num:
                        end_num = start_num + math.ceil(period_duration / (seg_dur / t.timescale)) - 1
                    for n in range(start_num, end_num + 1):
                        url = _replace_fields(seg_tmpl.get('media'),
                                               Bandwidth=rep.get('bandwidth'),
                                               Number=n,
                                               RepresentationID=rep.get('id'),
                                               Time=n)
                        t.segments.append((url, None))

            elif seg_list is not None:
                t.timescale = float(seg_list.get('timescale') or 1)
                init_elem = seg_list.find('Initialization')
                if init_elem is not None:
                    src = init_elem.get('sourceURL') or rep_base
                    if not re.match(r'^https?://', src, re.I):
                        src = urljoin(rep_base, src)
                    t.init_url = src
                    rng = init_elem.get('range')
                    if rng:
                        t.init_range = f'bytes={rng}'
                for su in seg_list.findall('SegmentURL'):
                    media = su.get('media') or rep_base
                    if not re.match(r'^https?://', media, re.I):
                        media = urljoin(rep_base, media)
                    media_rng = su.get('mediaRange')
                    t.segments.append((media, f'bytes={media_rng}' if media_rng else None))

            elif seg_base is not None:
                init_elem = seg_base.find('Initialization')
                if init_elem is not None:
                    rng = init_elem.get('range')
                    t.init_url = rep_base
                    if rng:
                        t.init_range = f'bytes={rng}'
                t.segments.append((rep_base, None))

            elif rep_base:
                t.segments.append((rep_base, None))

            tracks.append(t)

    return tracks


def parse_mpd(mpd_text: str, mpd_url: str) -> List[TrackInfo]:
    """
    MPD XML 을 파싱해 TrackInfo 목록 반환.
    멀티 Period 완전 지원:
      - 광고/로고 Period (non-DRM) 자동 필터링
      - 콘텐츠 Period 가 여럿이면 같은 해상도/언어 트랙의 segments 를 이어붙임
    """
    root = etree.fromstring(mpd_text.encode())
    for elem in root.iter():
        if '}' in elem.tag:
            elem.tag = elem.tag.split('}', 1)[1]

    all_periods = root.findall('Period')
    if not all_periods:
        logger.error('[downloader] MPD에 Period 없음')
        return []

    # MPD 전체 DRM 여부 (필터링 기준)
    mpd_has_drm = bool(root.findall('.//ContentProtection'))

    manifest_base = root.findtext('BaseURL') or mpd_url
    if not re.match(r'^https?://', manifest_base, re.I):
        manifest_base = urljoin(mpd_url, manifest_base)

    total_duration = _pt_to_sec(root.get('mediaPresentationDuration') or '')

    # 콘텐츠 Period 만 추려냄
    content_periods = []
    for period in all_periods:
        if _is_ad_period(period, mpd_has_drm):
            #logger.debug(f'[downloader] Period id={period.get("id")} 필터링 (광고/로고)')
            continue
        content_periods.append(period)

    if not content_periods:
        logger.error('[downloader] 유효한 콘텐츠 Period 없음')
        return []

    #logger.info(f'[downloader] 전체 Period={len(all_periods)}, 콘텐츠 Period={len(content_periods)}')

    # Period 가 하나면 단순 파싱
    if len(content_periods) == 1:
        period = content_periods[0]
        period_dur = _pt_to_sec(period.get('duration') or '') or total_duration
        return _parse_period_tracks(period, period_dur, manifest_base, mpd_url)

    # 멀티 Period: 첫 Period 기준 트랙 목록을 만들고
    # 이후 Period 의 동일 트랙 segments 를 이어붙임
    first_period = content_periods[0]
    first_dur = _pt_to_sec(first_period.get('duration') or '') or total_duration
    merged: List[TrackInfo] = _parse_period_tracks(first_period, first_dur, manifest_base, mpd_url)

    for period in content_periods[1:]:
        period_dur = _pt_to_sec(period.get('duration') or '') or total_duration
        extra = _parse_period_tracks(period, period_dur, manifest_base, mpd_url)

        for et in extra:
            # 같은 content_type + 해상도(비디오) 또는 언어(오디오)로 매칭
            for mt in merged:
                if mt.content_type != et.content_type:
                    continue
                if et.content_type == 'video' and mt.height != et.height:
                    continue
                if et.content_type == 'audio' and mt.language != et.language:
                    continue
                mt.segments.extend(et.segments)
                # DRM 정보는 있는 쪽 우선
                if not mt.is_drm and et.is_drm:
                    mt.is_drm = et.is_drm
                    mt.pssh_b64 = et.pssh_b64
                break

    return merged

CHUNK_SIZE = 1024 * 64  # 64KB per chunk

def _download_segment(session: requests.Session,
                      url: str,
                      headers: dict,
                      range_header: Optional[str],
                      out_path: Optional[pathlib.Path] = None,
                      retry: int = 3) -> bytes:
    """
    out_path 가 주어지면 청크 스트리밍으로 디스크에 직접 쓰고 b"" 반환.
    out_path 가 None 이면 기존처럼 bytes 반환 (init segment 용).
    """
    h = dict(headers)
    if range_header:
        h['Range'] = range_header

    for attempt in range(retry):
        try:
            r = session.get(url, headers=h, timeout=60, stream=True)
            r.raise_for_status()

            if out_path is not None:
                with open(out_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                return b""
            else:
                # init segment 등 메모리로 받아야 하는 경우
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        buf.extend(chunk)
                return bytes(buf)

        except Exception as e:
            logger.warning(f'[downloader] 세그먼트 재시도 {attempt+1}/{retry}: {e}')
            if out_path and out_path.exists():
                out_path.unlink(missing_ok=True)
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(f'세그먼트 다운로드 실패: {url}')


def _download_segments_parallel(session, segments, out_dir, headers,
                                  workers=4, progress_cb=None,
                                  stop_flag=None) -> List[pathlib.Path]:
    pad = len(str(len(segments)))
    files: List[Optional[pathlib.Path]] = [None] * len(segments)

    def _dl(idx_url_rng):
        idx, (url, rng) = idx_url_rng
        # stop_flag 체크 추가
        if stop_flag and stop_flag():
            raise InterruptedError('사용자 중단')
        fpath = out_dir / f'{idx:0{pad}d}.mp4'
        _download_segment(session, url, headers, rng, out_path=fpath)
        if progress_cb:
            progress_cb(idx + 1, len(segments))
        return idx, fpath

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_dl, (i, seg)): i for i, seg in enumerate(segments)}
        for fut in as_completed(futs):
            try:
                idx, fpath = fut.result()
                files[idx] = fpath
            except InterruptedError:
                # 나머지 future 취소
                for f in futs:
                    f.cancel()
                raise

    return [f for f in files if f]


def download_subtitle(session: requests.Session,
                       url: str,
                       lang: str,
                       video_filepath: str) -> None:
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        srt_path = pathlib.Path(video_filepath).with_suffix(f'.{lang}.srt')
        # vtt → srt 변환 시도
        try:
            import webvtt
            vtt = webvtt.from_buffer(BytesIO(r.content))
            with open(srt_path, 'w', encoding='utf-8') as f:
                vtt.write(f, format='srt')
        except Exception:
            srt_path.write_bytes(r.content)
    except Exception:
        logger.exception(f'[downloader] 자막 다운로드 실패: {url}')


def download_subtitles(subtitles: List[dict],
                        video_filepath: str,
                        wanted_langs: List[str],
                        session: Optional[requests.Session] = None) -> None:
    if not subtitles or not wanted_langs:
        return
    if session is None:
        session = requests.Session()
    for sub in subtitles:
        lang = sub.get('languagecode') or sub.get('language') or 'und'
        if 'all' not in wanted_langs and lang not in wanted_langs:
            continue
        url = sub.get('url') or sub.get('link')
        if not url:
            continue
        download_subtitle(session, url, lang, video_filepath)


def _select_best_video(tracks: List[TrackInfo], quality: str) -> Optional[TrackInfo]:
    """quality: '2160p' | '1080p' | '720p' 등."""
    target_h = {'2160p': 2160, '1080p': 1080, '720p': 720, '480p': 480}.get(quality, 1080)
    videos = [t for t in tracks if t.content_type == 'video']
    if not videos:
        return None
    # 원하는 해상도 이하 중 최고
    candidates = [v for v in videos if v.height <= target_h]
    if not candidates:
        candidates = videos
    return max(candidates, key=lambda v: (v.height, v.bandwidth))


def _select_best_audio(tracks: List[TrackInfo], lang: str = 'ko') -> Optional[TrackInfo]:
    audios = [t for t in tracks if t.content_type == 'audio']
    if not audios:
        return None
    # 언어 우선, 그 다음 bitrate
    preferred = [a for a in audios if a.language.startswith(lang)]
    return max(preferred or audios, key=lambda a: a.bandwidth)


class TvingDownloader:
    """
    wv_tool.WVDownloader 와 동일한 외부 인터페이스를 가진 티빙 전용 다운로더.

    config 키:
        callback_id           str   (필수)
        mpd_url               str   MPD or M3U8 URL
        streaming_protocol    str   'dash' | 'hls'
        output_filename       str   확장자 포함 파일명
        folder_output         str   최종 파일 저장 경로
        folder_tmp            str   임시 작업 경로
        drm                   bool  True 이면 license 필요
        license_url           str   Widevine 라이선스 서버 URL
        license_headers       dict  라이선스 요청 헤더
        mpd_headers           dict  MPD/세그먼트 요청 헤더
        quality               str   '1080p' (기본)
        clean                 bool  완료 후 tmp 정리 (기본 True)
        subtitles             list  [{'url':..., 'languagecode':...}]
        subtitle_langs        list  ['ko','en'] or ['all']
        keys                  list  [{'kid':..., 'key':...}]  (이미 키를 알 때)
    """

    STATUS_READY             = 'READY'
    STATUS_DOWNLOADING       = 'DOWNLOADING'
    STATUS_DECRYPTING        = 'DECRYPTING'
    STATUS_MERGING           = 'MERGING'
    STATUS_COMPLETED         = 'COMPLETED'
    STATUS_ERROR             = 'ERROR'
    STATUS_USER_STOP         = 'USER_STOP'
    STATUS_EXIST             = 'EXIST_OUTPUT_FILEPATH'
    STATUS_SEGMENT_FAIL      = 'SEGMENT_FAIL'

    def __init__(self, config: dict, callback_function: Optional[Callable] = None):
        self.config = config
        self._cb = callback_function
        self._stop_flag = False
        self.status = self.STATUS_READY
        self._thread: Optional[threading.Thread] = None

        self.callback_id       = config.get('callback_id', '')
        self.mpd_url           = config.get('mpd_url', '')
        self.protocol          = config.get('streaming_protocol', 'dash').lower()
        self.output_filename   = config.get('output_filename', 'output.mkv')
        self.folder_output     = pathlib.Path(config.get('folder_output', '/tmp'))
        self.folder_tmp        = pathlib.Path(config.get('folder_tmp', '/tmp'))
        self.is_drm            = bool(config.get('drm', False))
        self.quality           = config.get('quality', '1080p')
        self.clean             = config.get('clean', True)
        self.subtitles         = config.get('subtitles') or []
        self.subtitle_langs    = config.get('subtitle_langs') or ['ko']
        self.preset_keys       = config.get('keys') or []

        raw_hdrs = config.get('license_headers') or {}

        if 'header_key' in raw_hdrs and 'header_value' in raw_hdrs:
            # 신형: header_key/header_value 분리 형식 → 일반 헤더 dict 로 변환
            self.license_url     = config.get('license_url')
            self.license_headers = {
                'Content-Type': 'application/octet-stream',
                raw_hdrs['header_key']: raw_hdrs['header_value'],
            }
        else:
            # 구형: drm_license_uri / drm_key_request_properties dict 형식
            self.license_url     = config.get('license_url')
            self.license_headers = dict(raw_hdrs)

        self.mpd_headers = config.get('mpd_headers') or {}

        # 작업 ID (tmp 폴더 격리용)
        _id = hashlib.md5(self.callback_id.encode()).hexdigest()[:8]
        self._work_dir = self.folder_tmp / f'tving_{_id}'


    def start(self):
        """백그라운드 스레드로 다운로드 시작 (WVDownloader 와 동일)."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """다운로드 중단 요청."""
        self._stop_flag = True


    def set_status(self, status: str, **extra):
        self.status = status
        if self._cb:
            try:
                self._cb({'status': status, 'data': {'callback_id': self.callback_id, **extra}})
            except Exception:
                logger.exception('[downloader] 콜백 오류')


    def _run(self):
        try:
            self.set_status(self.STATUS_READY)
            out_path = self.folder_output / self.output_filename
            # 자막 유무에 따라 .mkv/.mp4 가 결정되므로 양쪽 모두 존재 확인
            if out_path.exists() or out_path.with_suffix('.mkv').exists() or out_path.with_suffix('.mp4').exists():
                logger.info(f'[downloader] 이미 존재함: {out_path.stem}')
                self.set_status(self.STATUS_EXIST)
                return

            self.folder_output.mkdir(parents=True, exist_ok=True)
            self._work_dir.mkdir(parents=True, exist_ok=True)

            session = self._make_session()

            if self.protocol == 'hls':
                result = self._download_hls(session, out_path)
            else:
                result = self._download_dash(session, out_path)

            if result:
                self.set_status(self.STATUS_COMPLETED)
            else:
                if self._stop_flag:
                    self.set_status(self.STATUS_USER_STOP)
                else:
                    self.set_status(self.STATUS_ERROR)

        except Exception:
            logger.error(f'[downloader] _run 예외:\n{traceback.format_exc()}')
            self.set_status(self.STATUS_ERROR)
        finally:
            if self.clean:
                self._cleanup()

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(self.mpd_headers)
        return s


    def _download_dash(self, session: requests.Session, out_path: pathlib.Path) -> bool:

        try:
            r = session.get(self.mpd_url, timeout=30)
            r.raise_for_status()
            mpd_text = r.text
        except Exception:
            logger.exception('TVING MPD 요청 실패')
            return False

        all_tracks = parse_mpd(mpd_text, self.mpd_url)
        if not all_tracks:
            logger.error('TVING MPD 에서 트랙을 찾지 못했습니다')
            return False

        video_track = _select_best_video(all_tracks, self.quality)
        audio_track = _select_best_audio(all_tracks)

        if video_track is None:
            logger.error('TVING 비디오 트랙 없음')
            return False

        self.set_status(self.STATUS_DOWNLOADING)

        video_raw = self._download_track(session, video_track, 'video')
        if video_raw is None:
            return False

        audio_raw = None
        if audio_track:
            audio_raw = self._download_track(session, audio_track, 'audio')

        # DRM 복호화
        keys = self._get_keys(video_track, session)
        video_final = self._maybe_decrypt(video_raw, keys, 'video')
        audio_final = None
        if audio_raw:
            audio_keys = keys if audio_track and audio_track.is_drm else []
            audio_final = self._maybe_decrypt(audio_raw, audio_keys, 'audio')

        # 자막 다운로드:
        # 1) MPD 내 text 트랙 (BaseURL 직접 방식)
        # 2) SupportTving API 가 별도로 준 self.subtitles 목록
        sub_paths = self._download_mpd_subtitles(session, all_tracks)
        if not sub_paths:
            sub_paths = self._download_subtitles_to_files(session)

        # 머지: 자막 있으면 .mkv, 없으면 .mp4
        self.set_status(self.STATUS_MERGING)
        audio_lang = audio_track.language if audio_track else 'und'
        result = self._merge(video_final, audio_final, sub_paths, out_path, audio_lang)
        return result

    def _download_track(self, session: requests.Session,
                         track: TrackInfo, label: str) -> Optional[pathlib.Path]:
        """트랙의 세그먼트를 내려받아 하나의 파일로 합친 뒤 경로 반환."""
        work_dir = self._work_dir / label
        work_dir.mkdir(parents=True, exist_ok=True)

        # init 다운로드
        init_data: Optional[bytes] = None
        if track.init_url:
            hdrs = {}
            if track.init_range:
                hdrs['Range'] = track.init_range
            try:
                init_data = _download_segment(session, track.init_url,
                                               self.mpd_headers, track.init_range)
                #logger.debug(f'[downloader] {label} init: {len(init_data)} bytes')
            except Exception:
                logger.exception(f'[downloader] {label} init 다운로드 실패')
                return None

        # SegmentBase 인 경우 media_range 를 Content-Range 에서 얻음
        segments = track.segments
        if (track.init_range and len(segments) == 1
                and segments[0][1] is None and init_data is not None):
            # init_data 를 Content-Range: bytes 0-N / TOTAL 에서 total 추출
            # 이미 init 요청 시 받았으니 media range 는 init_end+1 ~ total
            init_end = int(track.init_range.replace('bytes=', '').split('-')[1])
            # 별도 HEAD 요청으로 total 확인
            try:
                head_r = session.head(segments[0][0], timeout=10)
                total = int(head_r.headers.get('Content-Length', 0))
                if total > init_end + 1:
                    media_rng = f'bytes={init_end + 1}-{total - 1}'
                    segments = [(segments[0][0], media_rng)]
                    #logger.debug(f'[downloader] {label} SegmentBase media range: {media_rng}')
            except Exception:
                logger.warning(f'[downloader] {label} HEAD 실패, 전체 재요청')

        def _progress(done, total):
            if self._stop_flag:
                raise InterruptedError('사용자 중단')
            #logger.debug(f'[downloader] {label} {done}/{total}')

        try:
            seg_files = _download_segments_parallel(
                session, segments, work_dir, self.mpd_headers,
                workers=4,
                progress_cb=_progress,
                stop_flag=lambda: self._stop_flag,  # 추가
            )
        except InterruptedError:
            return None
        except Exception:
            logger.exception(f'[downloader] {label} 세그먼트 다운로드 실패')
            self.set_status(self.STATUS_SEGMENT_FAIL)
            return None

        # 합치기
        merged_path = self._work_dir / f'{label}_merged.mp4'
        with open(merged_path, 'wb') as mf:
            if init_data:
                mf.write(init_data)
            for sf in seg_files:
                mf.write(sf.read_bytes())

        return merged_path


    def _download_hls(self, session: requests.Session, out_path: pathlib.Path) -> bool:
        """HLS 는 ffmpeg 바이너리로 직접 다운로드한다."""
        try:
            from wv_tool.tool import FFMPEG
        except ImportError:
            FFMPEG = 'ffmpeg'

        self.set_status(self.STATUS_DOWNLOADING)
        # HLS는 자막 없이 단일 컨테이너로 받으므로 항상 .mp4
        tmp_out = self._work_dir / out_path.with_suffix('.mp4').name

        cmd = [str(FFMPEG), '-y']
        if self.mpd_headers:
            hdr_str = ''.join(f'{k}: {v}\r\n' for k, v in self.mpd_headers.items())
            cmd += ['-headers', hdr_str]
        cmd += [
            '-i', self.mpd_url,
            '-c', 'copy',
            str(tmp_out),
        ]

        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 or not tmp_out.exists():
            logger.error(f'[downloader] HLS ffmpeg 실패 (rc={r.returncode}):\n'
                         f'{r.stderr.decode(errors="ignore")}')
            return False

        final_path = out_path.with_suffix('.mp4')
        shutil.move(str(tmp_out), str(final_path))
        return True


    def _get_keys(self, video_track: TrackInfo,
                   session: requests.Session) -> List[dict]:
        """
        wv_tool.WVDecryptManager 로 Widevine 라이선스를 요청해
        키 목록 [{'kid': ..., 'key': ...}, ...] 을 반환한다.
        DRM 형식 정규화는 __init__ 에서 완료되므로 여기선 그대로 사용.
        """
        if self.preset_keys:
            logger.info(f'[downloader] preset_keys 사용: {len(self.preset_keys)}개')
            return self.preset_keys

        if not self.is_drm or not video_track.is_drm:
            return []

        if not self.license_url:
            logger.warning('[downloader] DRM이지만 license_url 없음')
            return []

        pssh_b64 = video_track.pssh_b64
        if not pssh_b64:
            logger.error('[downloader] PSSH 없음 – 라이선스 요청 불가')
            return []

        try:
            from wv_tool.manager import WVDecryptManager
            from base64 import b64encode

            wv = WVDecryptManager(pssh_b64)
            challenge = wv.get_challenge()

            lic_resp = requests.post(
                url=self.license_url,
                data=challenge,
                headers=self.license_headers,
                proxies=self.config.get('proxies') or {},
                timeout=30,
            )
            lic_resp.raise_for_status()

            license_b64 = b64encode(lic_resp.content)
            correct, keys = wv.get_result(license_b64)

            if not correct:
                logger.error('[downloader] WVDecryptManager.get_result() 실패')
                return []

            result = []
            for k in keys:
                parts = k.split(':')
                if len(parts) == 2:
                    result.append({'kid': parts[0], 'key': parts[1]})

            #logger.info(f'[downloader] 키 획득 완료: {len(result)}개 / {result}')
            return result

        except Exception:
            logger.exception('[downloader] 라이선스 요청 실패')
            return []

    def _maybe_decrypt(self, src: Optional[pathlib.Path],
                        keys: List[dict],
                        label: str) -> Optional[pathlib.Path]:
        """
        wv_tool.WVTool.mp4dump() 으로 KID 를 읽어 해당 key 를 찾아
        WVTool.mp4decrypt() 로 복호화한다.
        DRM 없으면 src 그대로 반환.
        """
        if src is None:
            return None
        if not keys:
            return src  # non-DRM

        try:
            from wv_tool.tool import WVTool
        except ImportError:
            logger.error('[downloader] wv_tool.tool 임포트 실패')
            return src

        self.set_status(self.STATUS_DECRYPTING)

        dump_path = src.with_suffix('.dump.txt')
        dst = src.with_suffix('.dec.mp4')

        try:
            # mp4dump 로 KID 추출
            WVTool.mp4dump(str(src), str(dump_path))
            dump_text = dump_path.read_text(errors='ignore')

            if 'default_KID = [' not in dump_text:
                # KID 없음 = 암호화 안 됨
                #logger.warning(f'[downloader] {label} KID 없음 – 복호화 스킵')
                return src

            kid = dump_text.split('default_KID = [')[1].split(']')[0].replace(' ', '')
            key = self._find_key(kid, keys)

            if not key:
                logger.error(f'[downloader] {label} KID {kid} 에 맞는 키 없음 / 보유키={keys}')
                return src

            WVTool.mp4decrypt(str(src), str(dst), kid, key)

            if dst.exists():
                #logger.info(f'[downloader] {label} 복호화 완료')
                return dst
            else:
                logger.error(f'[downloader] {label} 복호화 후 파일 없음')
                return src

        except Exception:
            logger.exception(f'[downloader] {label} 복호화 중 예외')
            return src
        finally:
            if dump_path.exists():
                dump_path.unlink(missing_ok=True)

    @staticmethod
    def _find_key(kid: str, keys: List[dict]) -> Optional[str]:
        """kid 문자열로 keys 리스트에서 key 값 반환."""
        kid_clean = kid.replace('-', '').lower()
        for k in reversed(keys):
            if k.get('kid', '').replace('-', '').lower() == kid_clean:
                return k.get('key')
        return None

    @staticmethod
    def _vtt_to_srt(vtt_path: pathlib.Path, srt_path: pathlib.Path) -> bool:
        """ffmpeg 로 vtt → srt 변환. 성공 시 True."""
        try:
            from wv_tool.tool import FFMPEG
        except ImportError:
            FFMPEG = 'ffmpeg'
        r = subprocess.run(
            [str(FFMPEG), '-y', '-i', str(vtt_path), str(srt_path)],
            capture_output=True,
        )
        return r.returncode == 0 and srt_path.exists()

    def _download_mpd_subtitles(self, session: requests.Session,
                                 all_tracks: List['TrackInfo']) -> List[pathlib.Path]:
        """
        parse_mpd 가 파싱한 TrackInfo 중 content_type=='text' 트랙을
        tmp 폴더에 srt 로 내려받아 경로 리스트 반환.
        subtitle_langs 필터 적용.
        """
        result: List[pathlib.Path] = []
        text_tracks = [t for t in all_tracks if t.content_type == 'text']
        if not text_tracks:
            return result

        for track in text_tracks:
            lang = track.language or 'und'
            if 'all' not in self.subtitle_langs and lang not in self.subtitle_langs:
                continue
            if not track.segments:
                continue

            url, rng = track.segments[0]
            try:
                data = _download_segment(session, url, self.mpd_headers, rng)
                ext = 'srt' if url.lower().endswith('.srt') else 'vtt'
                raw_path = self._work_dir / f'sub.{lang}.{ext}'
                raw_path.write_bytes(data)

                if ext == 'vtt':
                    srt_path = self._work_dir / f'sub.{lang}.srt'
                    if not self._vtt_to_srt(raw_path, srt_path):
                        logger.warning(f'[downloader] vtt→srt 변환 실패, vtt 그대로 사용: {lang}')
                        srt_path = raw_path
                    raw_path.unlink(missing_ok=True)
                else:
                    srt_path = raw_path

                result.append(srt_path)
                #logger.info(f'[downloader] MPD 자막 다운로드: {srt_path.name}')
            except Exception:
                logger.exception(f'[downloader] MPD 자막 다운로드 실패: {url[:80]}')

        return result

    def _download_subtitles_to_files(self, session: requests.Session) -> List[pathlib.Path]:
        """원하는 언어의 자막을 tmp 폴더에 srt 로 내려받아 경로 리스트 반환."""
        result: List[pathlib.Path] = []
        if not self.subtitles or not self.subtitle_langs:
            return result
        for sub in self.subtitles:
            lang = sub.get('languagecode') or sub.get('language') or 'und'
            if 'all' not in self.subtitle_langs and lang not in self.subtitle_langs:
                continue
            url = sub.get('url') or sub.get('link')
            if not url:
                continue
            try:
                r = session.get(url, timeout=30)
                r.raise_for_status()
                ext = 'srt' if url.lower().endswith('.srt') else 'vtt'
                raw_path = self._work_dir / f'sub.{lang}.{ext}'
                raw_path.write_bytes(r.content)

                if ext == 'vtt':
                    srt_path = self._work_dir / f'sub.{lang}.srt'
                    if not self._vtt_to_srt(raw_path, srt_path):
                        logger.warning(f'[downloader] vtt→srt 변환 실패, vtt 그대로 사용: {lang}')
                        srt_path = raw_path
                    raw_path.unlink(missing_ok=True)
                else:
                    srt_path = raw_path

                result.append(srt_path)
                logger.info(f'[downloader] 자막 다운로드: {srt_path.name}')
            except Exception:
                logger.exception(f'[downloader] 자막 다운로드 실패: {url}')
        return result

   
    def _merge(self,
               video: Optional[pathlib.Path],
               audio: Optional[pathlib.Path],
               subs: List[pathlib.Path],
               out_path: pathlib.Path,
               audio_lang: str = 'und') -> bool:

        if video is None:
            return False

        # WVTool 바이너리 경로 재사용
        try:
            from wv_tool.tool import FFMPEG, MKVMERGE
        except ImportError:
            FFMPEG = 'ffmpeg'
            MKVMERGE = 'mkvmerge'

        has_subs = bool(subs)
        final_path = out_path.with_suffix('.mkv' if has_subs else '.mp4')

        # ── .mkv : mkvmerge ──────────────────────────────────────────────
        if has_subs:
            cmd = [str(MKVMERGE), '-o', str(final_path), str(video)]
            if audio:
                cmd += ['--language', f'0:{audio_lang}', str(audio)]
            for sub in subs:
                lang = 'und'
                parts = sub.stem.split('.')  # ['sub', 'ko']
                if len(parts) >= 2:
                    lang = parts[-1]
                cmd += ['--language', f'0:{lang}', str(sub)]
            #logger.info(f'[downloader] mkvmerge 시작: {final_path.name}')
            r = subprocess.run(cmd, capture_output=True)
            # mkvmerge returncode 1 = warnings (정상)
            if r.returncode in (0, 1) and final_path.exists():
                #logger.info(f'[downloader] mkvmerge 완료: {final_path.name}')
                return True
            logger.error(f'[downloader] mkvmerge 실패 (rc={r.returncode}):\n'
                         f'{r.stderr.decode(errors="ignore")}')
            return False

        else:
            cmd = [str(FFMPEG), '-y', '-copyts',
                   '-i', str(video)]
            if audio:
                cmd += ['-i', str(audio)]
            cmd += ['-map', '0:v']
            if audio:
                cmd += ['-map', '1:a']
            cmd += ['-c:v', 'copy', '-c:a', 'copy', str(final_path)]
            #logger.info(f'[downloader] ffmpeg merge 시작: {final_path.name}')
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0 and final_path.exists():
                #logger.info(f'[downloader] ffmpeg merge 완료: {final_path.name}')
                return True
            logger.error(f'[downloader] ffmpeg 실패 (rc={r.returncode}):\n'
                         f'{r.stderr.decode(errors="ignore")}')
            return False


    def _cleanup(self):
        try:
            if self._work_dir.exists():
                shutil.rmtree(self._work_dir)
        except Exception:
            logger.warning(f'[downloader] 임시폴더 정리 실패: {self._work_dir}')

    @classmethod
    def get_list(cls):
        """mod_recent의 downloader.get_list() 호출 호환용. 빈 목록 반환."""
        return []