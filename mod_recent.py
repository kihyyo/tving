import threading
from support.expand.ffmpeg import SupportFfmpeg
from pathlib import Path
from support_site import SupportTving
from framework import app
from sqlalchemy import or_, desc
from collections import deque
from .setup import *
from .downloader import TvingDownloader, extract_drm_config, download_subtitles

name = 'recent'


class ModuleRecent(PluginModuleBase):

    def __init__(self, P):
        super(ModuleRecent, self).__init__(P, 'list', scheduler_desc="티빙 최근 방송 다운로드")
        self.name = name
        self.db_default = {
            f"{self.name}_db_version": "2",
            f"{P.package_name}_{self.name}_last_list_option": "",
            f"{self.name}_interval": "30",
            f"{self.name}_auto_start": "False",
            f"{self.name}_quality": "1080p",
            f"{self.name}_retry_user_abort": "False",
            f"{self.name}_qvod_download": "False",
            f"{self.name}_except_channel": "",
            f"{self.name}_except_category": "",
            f"{self.name}_except_program": "",
            f"{self.name}_except_episode_keyword": "특집,비하인드,스페셜,선공개,티저,메이킹,예고",
            f"{self.name}_except_episode_episodetitle": "예고",
            f"{self.name}_page_count": "2",
            f"{self.name}_save_path": "{PATH_DATA}"+os.sep+"download",
            f"{self.name}_download_program_in_qvod": "",
            f"{self.name}_download_mode": "blacklist",
            f"{self.name}_whitelist_category": "",
            f"{self.name}_whitelist_program": "",
            f"{self.name}_whitelist_first_episode_download": "True",
            f"{self.name}_ffmpeg_max_count": "4",
            f"{self.name}_2160_receive_1080": "False",
            f"{self.name}_2160_wait_minute": "100",
            f"{self.name}_auto_db_clear": "False",
            f"{self.name}_auto_db_days": "7",
            f"{self.name}_recent_days": "2",
            f"{self.name}_subtitle_langs": "ko",
            f"{self.name}_recent_keywords": "드라마,예능,교양,다큐멘터리,시사,애니메이션",
            f"{self.name}_genre_base_path": "{PATH_DATA}" + os.sep + "download",
            f"{self.name}_genre_path_targets": "",
        }
        self.web_list_model = ModelTvingRecent
    
    def process_menu(self, page_name, req):
        arg = P.ModelSetting.to_dict()
        if page_name == 'setting':
            arg['is_include'] = scheduler.is_include(self.get_scheduler_id())
            arg['is_running'] = scheduler.is_running(self.get_scheduler_id())
        return render_template(f'{P.package_name}_{name}_{page_name}.html', arg=arg)


    def process_command(self, command, arg1, arg2, arg3, req):
        ret = {'ret':'success'}
        if command == 'add_condition':
            mode = arg1
            value = arg2
            old_list = P.ModelSetting.get_list(mode)
            old_str = P.ModelSetting.get(mode)
            if value in old_list:
                ret['msg'] = "이미 설정되어 있습니다."
                ret['ret'] = "warning"
            else:
                if old_str != '':
                    old_str += ', '
                old_str += value
                P.ModelSetting.set(mode, old_str)
                ret['msg'] = "추가하였습니다."
        elif command == 'retrieve':
            # 상태 초기화 후 '해당 항목'을 직접 재검사/다운로드
            db_id = arg1
            item = ModelTvingRecent.get_by_id(db_id)
            if item is not None:
                item.completed = False
                item.user_abort = False
                item.pf_abort = False
                item.etc_abort = 0
                item.retry = 0
                item.save()
                # 수집 단계는 DB에 있는 항목을 스킵하므로, 누른 항목을
                # 곧장 process_single_vod 로 보내 재검사한다.
                threading.Thread(
                    target=self.retrieve_single,
                    args=(item.contentid, item.programid),
                    daemon=True,
                ).start()
                ret['msg'] = "상태를 초기화하고 해당 항목을 즉시 재검사합니다."
            else:
                ret['ret'] = 'error'
                ret['msg'] = "항목을 찾을 수 없습니다."
        elif command == 'reset_status':
            # 초기화: DB에서 해당 행을 삭제한다.
            # 수집 단계(get_recent_episodes)는 "DB에 없는" 에피소드를 신규로
            # 잡으므로, 삭제하면 다음 스케줄 스캔에서 다시 검사·다운로드된다.
            # (해당 회차가 recent_days 이내이고 프로그램의 최신 회차로 계속
            #  잡힐 때 재수집됨. 즉시 받으려면 '갱신'을 사용.)
            db_id = arg1
            item = ModelTvingRecent.get_by_id(db_id)
            if item is not None:
                ModelTvingRecent.delete_by_id(item.id)
                ret['msg'] = "DB에서 삭제했습니다. 다음 스캔 시 다시 검사합니다."
            else:
                ret['ret'] = 'error'
                ret['msg'] = "항목을 찾을 수 없습니다."
        elif command == 'delete':
            # 소프트 삭제: etc_abort=99로 마킹하여 목록에서 숨기고 재수집도 방지
            # (완전 삭제시 스케줄러가 동일 contentid를 재수집하여 다시 다운로드 시도하는 문제 방지)
            item = ModelTvingRecent.get_by_id(arg1)
            if item is not None:
                item.etc_abort = 99
                item.save()
                ret['msg'] = "삭제되었습니다."
            else:
                ret['ret'] = 'error'
                ret['msg'] = "항목을 찾을 수 없습니다." 
        return jsonify(ret)

    def migration(self):
        try:
            import sqlite3
            db_file = app.config['SQLALCHEMY_BINDS'][P.package_name].replace('sqlite:///', '').split('?')[0]
            #logger.error(db_file)
            if P.ModelSetting.get(f'{self.name}_db_version') == '1':
                connection = sqlite3.connect(db_file)
                cursor = connection.cursor()
                query = f'ALTER TABLE {P.package_name}_{name} ADD category1_name VARCHAR(255);'
                cursor.execute(query)
                connection.commit()
                connection.close()
                P.ModelSetting.set(f'{name}_db_version', '2')
                db.session.flush()
        except Exception as e:
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())

    def _safe_dirname(self, s: str) -> str:
        """폴더명으로 사용할 수 없는 문자 제거."""
        s = (s or '').strip()
        if not s:
            return '일반'
        return re.sub(r'[\\/:*?"<>|]', '_', s)

    def resolve_save_path(self, episode_db_item) -> str:
        """
        category1_name 이 genre_path_targets 에 포함되면
        genre_base_path/category 폴더를 반환. 아니면 save_path.
        wavve 플러그인과 동일한 구조.
        """
        base_save = ToolUtil.make_path(P.ModelSetting.get(f"{self.name}_save_path"))

        targets = P.ModelSetting.get_list(f"{self.name}_genre_path_targets", ',')
        targets = [t.strip() for t in targets if t and str(t).strip()]
        if not targets:
            return base_save

        category = (episode_db_item.category1_name or '').strip() if episode_db_item else ''
        if category and category in targets:
            base_genre = ToolUtil.make_path(
                P.ModelSetting.get(f"{self.name}_genre_base_path")
                or P.ModelSetting.get(f"{self.name}_save_path")
            )
            folder = self._safe_dirname(category)
            out = str(Path(base_genre) / folder)
            Path(out).mkdir(parents=True, exist_ok=True)
            return ToolUtil.make_path(out)

        return base_save

    def _prepare_run(self):
        """scheduler_function / retrieve_single 공통 실행 준비.
        설정값과 큐/이벤트를 초기화한다."""
        self.seen_program_codes = set()
        self.save_path = ToolUtil.make_path(P.ModelSetting.get(f"{self.name}_save_path"))
        self.quality = P.ModelSetting.get(f"{self.name}_quality")
        self.retry_user_abort = P.ModelSetting.get_bool(f"{self.name}_retry_user_abort")
        self.qvod_download = P.ModelSetting.get_bool(f"{self.name}_qvod_download")
        self.except_channel = P.ModelSetting.get_list(f"{self.name}_except_channel", ',')
        self.except_category = P.ModelSetting.get_list(f"{self.name}_except_category", ',')
        self.except_program = P.ModelSetting.get_list(f"{self.name}_except_program", ',')
        self.download_program_in_qvod = P.ModelSetting.get_list(f"{self.name}_download_program_in_qvod", ',')
        self.download_mode = P.ModelSetting.get(f"{self.name}_download_mode")
        self.whitelist_category = P.ModelSetting.get_list(f"{self.name}_whitelist_category", ',')
        self.whitelist_program = P.ModelSetting.get_list(f"{self.name}_whitelist_program", ',')
        self.whitelist_first_episode_download =  P.ModelSetting.get_bool(f"{self.name}_whitelist_first_episode_download")
        self.except_episode_keyword = P.ModelSetting.get_list(f"{self.name}_except_episode_keyword", ',')
        self.except_episode_episodetitle = P.ModelSetting.get_list(f"{self.name}_except_episode_episodetitle", ',')

        self.vod_queue = deque()
        self._dl_event = threading.Event()
        self._dl_event.set()

    def retrieve_single(self, contentid, programid=None):
        """갱신/초기화 버튼: 클릭한 항목 하나를 직접 재검사한다.
        스케줄러의 수집 단계(get_recent_episodes)는 DB에 이미 있는 항목을
        건너뛰므로, 버튼으로 누른 항목은 수집을 거치지 않고 곧장
        process_single_vod 로 보내 재검사/다운로드한다."""
        try:
            self._prepare_run()
            vod = {
                'episode': {'code': contentid},
                'program': {'code': programid or ''},
            }
            self.process_single_vod(vod)
            # 다운로드가 시작됐다면 완료까지 대기
            self._dl_event.wait()
        except Exception:
            P.logger.error('[recent] retrieve_single 실패')
            P.logger.error(traceback.format_exc())

    def scheduler_function(self):
        #P.logger.error("scheduler_function")
        if P.ModelSetting.get_bool(f"{self.name}_auto_db_clear"):
            ModelTvingRecent.delete_all(P.ModelSetting.get_int(f"{self.name}_auto_db_days"))

        self._prepare_run()

        recent_days = P.ModelSetting.get_int(f"{self.name}_recent_days") or 2
        recent_keywords = P.ModelSetting.get_list(f"{self.name}_recent_keywords", ',')
        if not recent_keywords:
            P.logger.info('[recent] recent_keywords 미설정, 수집 스킵')
            return
        try:
            recent_episodes = SupportTving.get_recent_episodes(
                recent_days=recent_days,
                keywords=recent_keywords,
                model=ModelTvingRecent,
            )
            # get_recent_episodes 반환값을 process_single_vod 가 기대하는
            # vod dict 형식으로 변환해 큐에 넣음
            for ep in reversed(recent_episodes):
                self.vod_queue.appendleft({
                    'episode': {'code': ep['episode_code']},
                    'program': {'code': ep['program_code']},
                    '_recent_meta': ep,
                })
            P.logger.info(f'[recent] 신규 에피소드 {len(recent_episodes)}건 수집')
        except Exception:
            P.logger.exception('[recent] get_recent_episodes 실패')
            
        while self.vod_queue:
            vod = self.vod_queue.popleft()
            self.process_single_vod(vod)

        # 마지막 다운로드 완료 대기
        self._dl_event.wait()
       
    def process_single_vod(self, vod):
        try:
            purge = False  # QVOD 등 DB에 마커를 남기면 안 되는 항목 표시용
            self._dl_event.wait()
            code = vod["episode"]["code"]
            #P.logger.debug(f"[{vod['episode']['name']['ko']}] [{vod['episode']['frequency']}]")
            episode_db_item = ModelTvingRecent.get_episode_by_recent(code)

            if episode_db_item is not None:
                if episode_db_item.completed:
                    return
                elif episode_db_item.user_abort:
                    if self.retry_user_abort:
                        episode_db_item.user_abort = False
                    else:
                        #사용자 중지로 중단했고, 다시받기가 false이면 패스
                        return
                elif episode_db_item.etc_abort > 11:
                    # 1:알수없는이유 시작실패, 2 타임오버, 3, 강제스톱.킬
                    # 11: QVOD, 12:제외채널, 13:제외프로그램
                    # 13:장르제외, 14:화이트리스트 제외, 7:권한없음, 6:화질다름
                    #P.logger.debug('ETC ABORT:%s', episode_db_item.etc_abort)
                    return
                elif episode_db_item.retry > 20:
                    P.logger.warning('retry 20')
                    episode_db_item.etc_abort = 9
                    return
            # URL때문에 DB에 있어도 다시 JSON을 받아야함.
            #db_item.contents_json['drm'] == ''

            for episode_try in range(3):
                contents_json = SupportTving.get_info(code, self.quality)
                if contents_json is None:
                    P.logger.debug('episode fail.. %s', episode_try)
                    time.sleep(20)
                else:
                    break
            
            action = "dash" if not 'm3u8' in contents_json['url'] else "hls"
            url = contents_json['url']
            
            if episode_db_item is None:
                # get_recent_episodes 로 받은 최소 구조 vod 를
                # contents_json['content'] (플랫 구조) 로 보강
                if 'channel' not in vod or not isinstance(vod.get('channel'), dict):
                    try:
                        c = contents_json.get('content') or {}
                        vod = {
                            'channel': {
                                'name': {'ko': c.get('channel', '')},
                            },
                            'program': {
                                'code':           c.get('program_code', ''),
                                'name':           {'ko': c.get('title', '')},
                                'category1_name': {'ko': c.get('category_name', '')},
                                'image':          c.get('image') or [],
                            },
                            'episode': {
                                'code':           c.get('episode_code', code),
                                'broadcast_date': c.get('episode_broad_dt', ''),
                                'frequency':      c.get('frequency', ''),
                                'name':           {'ko': c.get('episode_title', '')},
                                'image':          c.get('episode_image') or [],
                            },
                        }
                    except Exception as e:
                        P.logger.warning(f'[recent] vod 재구성 실패: {e}')
                episode_db_item = ModelTvingRecent('recent', info=vod, contents=contents_json)
                categories = vod['program'].get('display_category2', [])
                for c in categories:
                    if c.startswith(('POS005', 'POS006', 'PCAN', 'PCC')):
                        program_code = vod['program']['code']
                        if program_code not in self.seen_program_codes:
                            try:
                                full_list = SupportTving.get_vod_list(program_code)['result']
                                queued_codes = set(item['episode']['code'] for item in self.vod_queue)
                                for item in full_list:
                                    ep_code = item['episode']['code'].strip()
                                    if (ep_code != code.strip() and ep_code not in queued_codes and ModelTvingRecent.get_episode_by_recent(ep_code) is None):
                                        P.logger.debug(f"추가 수집: {ep_code}")
                                        self.vod_queue.appendleft(item)
                                self.seen_program_codes.add(program_code)
                            except Exception as e:
                                P.logger.warning(f"🔁 프로그램 전체 탐색 실패: {program_code} / {e}")
                        break 
            else:
                if contents_json is None:
                    return
                else:
                    episode_db_item.set_streaming(contents_json)
            if contents_json['url'].find('preview') != -1:
                episode_db_item.etc_abort = 7
                return

            # qvod 체크
            is_qvod = 'quickvod' in url or ('start=' in url and 'end=' in url)                       
            # 채널, 프로그램 체크
            flag_download = True
            
            if is_qvod:
                if not self.qvod_download:
                    episode_db_item.etc_abort = 11
                    flag_download = False
                    for programtitle in self.download_program_in_qvod:
                        if episode_db_item.programtitle.replace(' ', '').find(programtitle.replace(' ', '')) != -1:
                            flag_download = True
                            episode_db_item.etc_abort = 0
                            break
                    # 다운로드 대상이 아닌 순수 QVOD → DB에 흔적을 남기지 않는다.
                    # finally 에서 save() 대신 기존 행을 삭제(purge)하도록 한다.
                    # 정식 VOD 로 전환되면 다음 스캔에서 신규 항목처럼 재검사된다.
                    if not flag_download:
                        purge = True
                        return
                # 시간체크
                if flag_download:
                    match = re.compile(r'Quick\sVOD\s(?P<time>\d{2}\:\d{2})\s').search(episode_db_item.episodetitle)
                    if match:
                        dt_now = datetime.now()
                        dt_tmp = datetime.strptime(match.group('time'), '%H:%M')
                        dt_start = datetime(dt_now.year, dt_now.month, dt_now.day, dt_tmp.hour, dt_tmp.minute, 0, 0)
                        if (dt_now - dt_start).seconds < 0:
                            dt_start = dt_start + timedelta(days=-1)
                        qvod_playtime = episode_db_item.contents_json['playtime']
                        delta = (dt_now - dt_start).seconds
                        if int(qvod_playtime) > delta:
                            flag_download = False
                            episode_db_item.etc_abort = 8
                    else:
                        flag_download = False
                        episode_db_item.etc_abort = 7
            else:
                episode_db_item.filename = episode_db_item.filename.replace('-STQ.mp4', '-ST.mp4')

            if self.download_mode == 'blacklist':
                for program_name in self.except_program:
                    if episode_db_item.programtitle.replace(' ', '').find(program_name.replace(' ', '')) != -1:
                        episode_db_item.etc_abort = 13
                        flag_download = False
                        break
                if episode_db_item.channelname in self.except_channel:
                    episode_db_item.etc_abort = 12
                    flag_download = False
                if episode_db_item.category1_name in self.except_category:
                        episode_db_item.etc_abort = 17
                        flag_download = False
            else:
                if flag_download:
                    find_in_whitelist = False
                    for category_name in self.whitelist_category:
                        if episode_db_item.category1_name.replace(' ', '').find(category_name.replace(' ', '')) != -1:
                            find_in_whitelist = True
                            break
                    for program_name in self.whitelist_program:
                        if episode_db_item.programtitle.replace(' ', '').find(program_name.replace(' ', '')) != -1:
                            find_in_whitelist = True
                            break
                    if not find_in_whitelist:
                        episode_db_item.etc_abort = 14
                        flag_download = False
                if not flag_download and self.whitelist_first_episode_download and episode_db_item.episodenumber == 1:
                    flag_download = True
            # 2021-06-26
            if flag_download and episode_db_item.episodenumber is not None and episode_db_item.episodenumber != '':
                for keyword in self.except_episode_keyword:
                    if str(episode_db_item.episodenumber).find(keyword) != -1:
                        episode_db_item.etc_abort = 15
                        flag_download = False
                        break


            # 2022-08-18
            if flag_download and episode_db_item.episodenumber is not None and episode_db_item.episodenumber == '' and episode_db_item.episodetitle != None and episode_db_item.episodetitle != '':
                for keyword in self.except_episode_episodetitle:
                    if episode_db_item.episodetitle.find(keyword) != -1:
                        episode_db_item.etc_abort = 16
                        flag_download = False
                        break

            if flag_download and episode_db_item.quality != self.quality:
                if self.quality == '2160p' and episode_db_item.quality == '1080p' and P.ModelSetting.get_bool('recent_2160_receive_1080'):
                    if episode_db_item.created_time + timedelta(minutes=P.ModelSetting.get_int('recent_2160_wait_minute')) < datetime.now():
                        pass
                    else:
                        episode_db_item.etc_abort = 5
                        #db.session.commit()
                        return
                else:
                    episode_db_item.etc_abort = 6
                    #db.session.commit()
                    return

            if flag_download:
                episode_db_item.etc_abort = 0
                episode_db_item.pf = 0 # 재시도
                episode_db_item.save_path = self.resolve_save_path(episode_db_item)
                episode_db_item.start_time = datetime.now()
            else:
                return
            episode_db_item.save()

            # ── TvingDownloader 로 다운로드 ──────────────────────────────
            drm_cfg = extract_drm_config(contents_json)
            subtitles = []
            try:
                subtitles = contents_json['stream']['subtitles'] or []
            except Exception:
                pass

            downloader = TvingDownloader({
                'callback_id':          f"{P.package_name}_{self.name}_{episode_db_item.id}",
                'streaming_protocol':   "dash" if 'm3u8' not in contents_json['url'] else "hls",
                'drm':                  contents_json['drm'],
                'output_filename':      episode_db_item.filename,
                'quality':              self.quality,
                'folder_tmp':           os.path.join(F.config['path_data'], 'tmp'),
                'folder_output':        episode_db_item.save_path,
                'subtitles':            subtitles,
                'subtitle_langs':       P.ModelSetting.get_list(f"{self.name}_subtitle_langs", ',') or ['ko'],
                'clean':                True,
                **drm_cfg,
            }, callback_function=self.wvtool_callback_function)
            self._dl_event.clear()
            try:
                downloader.start()
            except Exception:
                self._dl_event.set()
                raise
        except Exception as e:
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())
        finally:
            if episode_db_item is not None:
                if purge:
                    # 순수 QVOD 스킵 항목: 저장하지 않는다.
                    # 이미 DB에 저장된 행이면 삭제하여 'seen' 마커를 없앤다.
                    try:
                        if getattr(episode_db_item, 'id', None) is not None:
                            ModelTvingRecent.delete_by_id(episode_db_item.id)
                    except Exception:
                        P.logger.error('[recent] QVOD purge 실패')
                        P.logger.error(traceback.format_exc())
                else:
                    episode_db_item.save()

    def db_delete(self, day):
        return ModelTvingRecent.delete_all(day=day)


    def ffmpeg_listener(self, **arg):
        episode = None
        refresh_type = None
        if arg['type'] == 'status_change':
            if arg['status'] == SupportFfmpeg.Status.DOWNLOADING:
                if arg['callback_id'].startswith('tving_recent'):
                    db_id = arg['callback_id'].split('_')[-1]
                    episode = ModelTvingRecent.get_by_id(db_id)
                if episode:
                    episode.ffmpeg_status = int(arg['status'])
                    episode.duration = arg['data']['duration']
                    episode.save()
            elif arg['status'] == SupportFfmpeg.Status.COMPLETED:
                pass
            elif arg['status'] == SupportFfmpeg.Status.READY:
                pass
        elif arg['type'] == 'last':
            if arg['callback_id'].startswith('tving_recent'):
                db_id = arg['callback_id'].split('_')[-1]
                episode = ModelTvingRecent.get_by_id(db_id)

            if episode:
                episode.ffmpeg_status = int(arg['status'])
                if arg['status'] == SupportFfmpeg.Status.WRONG_URL or arg['status'] == SupportFfmpeg.Status.WRONG_DIRECTORY or arg['status'] == SupportFfmpeg.Status.ERROR or arg['status'] == SupportFfmpeg.Status.EXCEPTION:
                    episode.etc_abort = 1
                elif arg['status'] == SupportFfmpeg.Status.USER_STOP:
                    episode.user_abort = True
                    logger.debug('Status.USER_STOP received..')
                elif arg['status'] == SupportFfmpeg.Status.COMPLETED:
                    episode.completed = True
                    episode.end_time = datetime.now()
                    episode.download_time = (episode.end_time - episode.start_time).seconds
                    episode.filesize = arg['data']['filesize']
                    episode.filesize_str = arg['data']['filesize_str']
                    episode.download_speed = arg['data']['download_speed']
                    logger.debug('Status.COMPLETED received..')
                elif arg['status'] == SupportFfmpeg.Status.TIME_OVER:
                    episode.etc_abort = 2
                elif arg['status'] == SupportFfmpeg.Status.PF_STOP:
                    episode.pf = int(arg['data']['current_pf_count'])
                    episode.pf_abort = 1
                elif arg['status'] == SupportFfmpeg.Status.FORCE_STOP:
                    episode.etc_abort = 3
                elif arg['status'] == SupportFfmpeg.Status.HTTP_FORBIDDEN:
                    episode.etc_abort = 4
                episode.save()
                logger.debug('LAST commit %s', arg['status'])
                self.current_download_count -= 1
        elif arg['type'] == 'log':
            pass
        elif arg['type'] == 'normal':
            pass
        if refresh_type is not None:
            pass


    def wvtool_callback_function(self, args):
        """TvingDownloader 콜백. args = {'status': ..., 'data': {'callback_id': ...}}"""
        try:
            db_item = ModelTvingRecent.get_by_id(args['data']['callback_id'].split('_')[-1])
        except Exception:
            return

        if db_item is None:
            return

        status = args['status']
        is_last = True

        try:
            if status in ("READY", "SEGMENT_FAIL"):
                is_last = False
            elif status == "DOWNLOADING":
                is_last = False
            elif status in ("DECRYPTING", "MERGING"):
                is_last = False
            elif status == "USER_STOP":
                db_item.user_abort = True
                db_item.save()
            elif status in ("EXIST_OUTPUT_FILEPATH", "COMPLETED"):
                db_item.completed = True
                db_item.end_time = datetime.now()
                if db_item.start_time:
                    db_item.download_time = (db_item.end_time - db_item.start_time).seconds
                db_item.save()
                P.logger.info(f'[TVING recent] 완료: {db_item.contentid}')
            elif status == "ERROR":
                db_item.etc_abort = 1
                db_item.save()
                P.logger.error(f'[TVING recent] 다운로드 오류: {db_item.contentid}')
            else:
                P.logger.debug(f'[TVING recent] 알 수 없는 상태 {status}: {db_item.contentid}')
        except Exception:
            P.logger.exception(f'[TVING recent] 콜백 처리 오류 (status={status})')
        finally:
            if is_last:
                self._dl_event.set()


class ModelTvingRecent(ModelBase):
    P = P
    __tablename__ = f'{P.package_name}_recent'
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)

    recent_json = db.Column(db.JSON)
    contents_json = db.Column(db.JSON)
    streaming_json = db.Column(db.JSON)

    contentid = db.Column(db.String)
    content_type = db.Column(db.String)  # movie, episode
    quality = db.Column(db.String)
    call = db.Column(db.String) # normal, recent, program
    drm = db.Column(db.Boolean)

    channelname = db.Column(db.String)
    programid = db.Column(db.String)
    programtitle = db.Column(db.String)
    releasedate = db.Column(db.String)
    episodenumber = db.Column(db.String)
    episodetitle = db.Column(db.String)
    category1_name = db.Column(db.String)

    image = db.Column(db.String)
    playurl = db.Column(db.String)
    filename = db.Column(db.String)
    duration = db.Column(db.Integer)
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    download_time = db.Column(db.Integer)
    completed = db.Column(db.Boolean)
    user_abort = db.Column(db.Boolean)
    pf_abort = db.Column(db.Boolean)
    etc_abort = db.Column(db.Integer) #ffmpeg 원인 1, 채널, 프로그램
    ffmpeg_status = db.Column(db.Integer)
    temp_path = db.Column(db.String)
    save_path = db.Column(db.String)
    pf = db.Column(db.Integer)
    retry = db.Column(db.Integer)
    filesize = db.Column(db.Integer)
    filesize_str = db.Column(db.String)
    download_speed = db.Column(db.String)


    def __init__(self, call, info, contents):
        self.created_time = datetime.now()
        self.call = call
        self.content_type = 'episode'
        self.completed = False
        self.user_abort = False
        self.pf_abort = False
        self.etc_abort = 0
        self.ffmpeg_status = -1
        self.pf = 0
        self.retry = 0
        self.contents_json = contents
        self.quality = P.ModelSetting.get(f"{name}_quality")
        self.drm = self.contents_json['drm']
        self.set_info(info)
        self.set_streaming(contents)


    def set_info(self, data):
        self.recent_json = data
        self.channelname = (data.get('channel') or {}).get('name', {}).get('ko', '')
        self.programid = (data.get('program') or {}).get('code', '')
        self.programtitle = (data.get('program') or {}).get('name', {}).get('ko', '')
        self.category1_name_ko = (data.get('program') or {}).get('category1_name', {}).get('ko', None)
        c1 = self.category1_name_ko or ''

        # 해외시리즈 판별:
        # 1순위: display_category1/2 의 POS 코드 (frequency API 사용 시 포함)
        # 2순위: 국내 방송사 목록에 없는 채널 (episodes API 사용 시 폴백)
        DOMESTIC_CHANNELS = {
            'KBS1', 'KBS2', 'KBS Joy', 'KBS Drama',
            'MBC', 'MBC every1', 'MBC에브리원', 'MBC스포츠+',
            'SBS', 'SBS Plus', 'SBS funE', 'SBS FiL', 'SBS CNBC',
            'tvN', 'tvN STORY', 'tvN SHOW',
            'JTBC', 'JTBC2', 'JTBC3 Fox Sports',
            'MBN', 'TV CHOSUN', 'TV조선', '채널A', 'Channel A',
            'OCN', 'ENA', 'OBS', 'EBS1', 'EBS2', 'EBS English',
            'TVING', '스튜디오지니',
            '연합뉴스TV', 'YTN',
        }
        program = data.get('program') or {}
        disp1 = program.get('display_category1') or []
        disp2 = program.get('display_category2') or []
        if isinstance(disp1, str):
            disp1 = [disp1]
        if isinstance(disp2, str):
            disp2 = [disp2]
        pos_flags = {'POS007', 'POS008', 'POS005', 'POS006', 'PCPOS'}
        has_pos = bool(pos_flags.intersection(set(disp1) | set(disp2)))

        if disp1 or disp2:
            # frequency API: display_category 있음 → POS 코드로 판별
            is_overseas = '드라마' in c1 and has_pos
        else:
            # episodes API: display_category 없음 → 채널명으로 판별
            is_overseas = '드라마' in c1 and self.channelname not in DOMESTIC_CHANNELS

        self.category1_name = '해외시리즈' if is_overseas else self.category1_name_ko

        ep = data.get('episode') or {}
        self.contentid = ep.get('code', '')
        self.releasedate = ep.get('broadcast_date', '')
        self.episodenumber = ep.get('frequency', '')
        self.episodetitle = (ep.get('name') or {}).get('ko', '')
        ep_images = ep.get('image') or []
        prog_images = (data.get('program') or {}).get('image') or []
        if ep_images:
            self.image = 'https://image.tving.com' + ep_images[0]['url']
        elif prog_images:
            self.image = 'https://image.tving.com' + prog_images[0]['url']
        else:
            self.image = ''

    def set_streaming(self, data):
        self.streaming_json = data
        if data != None:
            from support_site import SupportTving
            self.filename = SupportTving.get_filename(self.contents_json)
            self.playurl = data['url']
            #self.playurl = data['playurl']

    @classmethod
    def get_episode_by_recent(cls, code):
        with F.app.app_context():
            episode = db.session.query(cls) \
                .filter((cls.call == 'recent') | (cls.call == None)) \
                .filter_by(contentid=code) \
                .with_for_update().first()
            return episode

    @classmethod
    def delete_by_id(cls, _id):
        """id 로 행을 즉시 삭제한다.
        QVOD 스킵 항목이 DB에 'seen' 마커로 남아 재검사를 막는 것을
        방지하기 위해 사용한다."""
        with F.app.app_context():
            obj = db.session.query(cls).filter_by(id=_id).first()
            if obj is not None:
                db.session.delete(obj)
                db.session.commit()


    # 오버라이딩
    @classmethod
    def make_query(cls, req, order='desc', search='', option1='all', option2='all'):

        with F.app.app_context():
            query = F.db.session.query(cls)

            if search is not None and search != '':
                query = cls.make_query_search(query, search, cls.programtitle)

                #query = query.filter(or_(cls.programtitle.like('%'+search+'%'), cls.channelname.like('%'+search+'%')))
            if option1 == 'completed':
                query = query.filter_by(completed=True)
            elif option1 == 'uncompleted':
                query = query.filter_by(completed=False)
            elif option1 == 'user_abort':
                query = query.filter_by(user_abort=True)
            elif option1 == 'pf_abort':
                query = query.filter_by(pf_abort=True)
            elif option1 == 'etc_abort_under_10':
                query = query.filter(cls.etc_abort < 10, cls.etc_abort > 0)
            elif option1 == 'etc_abort_11':
                query = query.filter_by(etc_abort='11')
            elif option1 == 'etc_abort_12':
                query = query.filter_by(etc_abort='12')
            elif option1 == 'etc_abort_13':
                query = query.filter_by(etc_abort='13')
            elif option1 == 'etc_abort_14':
                query = query.filter_by(etc_abort='14')
            elif option1 == 'etc_abort_15':
                #query = query.filter_by(etc_abort='15')
                query = query.filter(or_(cls.etc_abort=='15', cls.etc_abort=='16'))

            # 소프트 삭제 항목(etc_abort=99) 기본 제외
            query = query.filter(cls.etc_abort != 99)

            if order == 'desc':
                query = query.order_by(desc(cls.id))
            else:
                query = query.order_by(cls.id)

            return query

    @classmethod
    def get_programs_and_episodes_by_date(cls, days_ago=8):

        with F.app.app_context():
            now = datetime.now()
            target_date = (now - timedelta(days=days_ago)).date()
            dt_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
            dt_end   = dt_start + timedelta(days=1)

            from .mod_program import ModelTvingProgram

            # 1) 기준일에 등장한 program_id 모으기 (recent + program)
            q_recent_pids = db.session.query(cls.programid).filter(
                cls.created_time >= dt_start,
                cls.created_time < dt_end,
                or_(cls.call == 'recent', cls.call == None)
            )

            q_program_pids = db.session.query(ModelTvingProgram.program_id).filter(
                ModelTvingProgram.created_time >= dt_start,
                ModelTvingProgram.created_time < dt_end
            )
            rows_program = q_program_pids.all()

            # DISTINCT program_id 집합
            program_ids = {
                row[0] for row in q_recent_pids.union(q_program_pids).all()
                if row[0]
            }

            if not program_ids:
                return {}

            # 2) 기간 제한 없이, 해당 program_id 들의 모든 에피소드 코드 가져오기
            rows_recent = db.session.query(
                cls.programid.label('program_id'),
                cls.contentid.label('episode_code')
            ).filter(
                cls.programid.in_(program_ids),
                or_(cls.call == 'recent', cls.call == None)
            ).all()

            rows_program = db.session.query(
                ModelTvingProgram.program_id.label('program_id'),
                ModelTvingProgram.episode_code.label('episode_code')
            ).filter(
                ModelTvingProgram.program_id.in_(program_ids)
            ).all()

            # 3) 하나의 dict로 합치기
            result = {}
            for program_id, episode_code in rows_recent + rows_program:
                if not program_id or not episode_code:
                    continue
                if program_id not in result:
                    result[program_id] = set()
                result[program_id].add(episode_code)

            return result