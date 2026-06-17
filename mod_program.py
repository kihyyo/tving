from support.expand.ffmpeg import SupportFfmpeg

from support_site import SupportTving

from .setup import *
from .downloader import TvingDownloader

name = 'program'


class ModuleProgram(PluginModuleBase):
    recent_code = None
    download_queue = None
    download_thread = None
    current_ffmpeg_count = 0

    def __init__(self, P):
        super(ModuleProgram, self).__init__(P, 'list')
        self.name = name
        self.db_default = {
            f"{P.package_name}_{self.name}_last_list_option": "",
            f"{self.name}_db_version": "1",
            f"{self.name}_recent_code": "",
            f"{self.name}_save_path": "{PATH_DATA}"+os.sep+"download",
            f"{self.name}_make_program_folder": "False",
            f"{self.name}_ffmpeg_max_count": "4",
            f"{self.name}_quality": "1080p",
            f"{self.name}_failed_redownload": "False",
            f"{self.name}_subtitle_langs": "ko",
        }
        self.web_list_model = ModelTvingProgram
        default_route_socketio_module(self, attach='/queue')
        self.previous_analyze = None

    def process_menu(self, page_name, req):
        arg = P.ModelSetting.to_dict()
        if page_name == 'select':
            arg["code"] = request.args.get('code')
            if arg['code'] is None:
                arg['code'] = P.ModelSetting.get(f"{self.name}_recent_code")
        return render_template(f'{P.package_name}_{name}_{page_name}.html', arg=arg)


    def process_command(self, command, arg1, arg2, arg3, req):
        ret = {'ret':'success'}
        if command == 'analyze':
            ret = self.get_module('basic').analyze(arg1)
            P.ModelSetting.set(f"{self.name}_recent_code", arg1)
            self.previous_analyze = ret
        elif command == 'previous_analyze':
            ret['data'] = self.previous_analyze
        # elif command == 'get_contents':
        #     ret = SupportTving.get_contents(arg1)
        elif command == 'program_page':
            data = SupportTving.get_vod_list(arg1, page=int(arg2))
            ret =  {'url_type': 'program', 'page':arg2, 'code':arg1, 'data' : data}
        elif command == 'download_program':
            _pass = arg3
            db_item = ModelTvingProgram.get(arg1, arg2)
            if _pass == 'false' and db_item != None:
                ret['ret'] = 'warning'
                ret['msg'] = '이미 DB에 있는 항목입니다.'
            elif _pass == 'true' and db_item != None and ModelTvingProgram.get_by_id_in_queue(db_item.id) != None:
                ret['ret'] = 'warning'
                ret['msg'] = '이미 큐에 있는 항목입니다.'
            else:
                if db_item == None:
                    db_item = ModelTvingProgram(arg1, arg2)
                    db_item.save()
                db_item.init_for_queue()
                self.download_queue.put(db_item)
                ret['msg'] = '다운로드를 추가 하였습니다.'
        elif command == 'download_program_check':
            lists = arg1[:-1].split(',')
            count = 0
            for _ in lists:
                code, quality = _.split('|')
                db_item = ModelTvingProgram(code, quality)
                db_item.save()
                db_item.init_for_queue()
                self.download_queue.put(db_item)
            ret['msg'] = f"{len(lists)}개를 추가 하였습니다."
        elif command == 'queue_list':
            ret = [x.as_dict_for_queue() for x in ModelTvingProgram.queue_list]
        elif command == 'program_list_command':
            if arg1 == 'remove_completed':
                count = ModelTvingProgram.remove_all(True)
                ret['msg'] = f"{count}개를 삭제하였습니다."
            elif arg1 == 'remove_incomplete':
                count = ModelTvingProgram.remove_all(False)
                ret['msg'] = f"{count}개를 삭제하였습니다."
            elif arg1 == 'add_incomplete':
                count = self.retry_download_failed()
                ret['msg'] = f"{count}개를 추가 하였습니다."
            elif arg1 == 'remove_one':
                result = ModelTvingProgram.delete_by_id(arg2)
                if result:
                    ret['msg'] = '삭제하였습니다.'
                else:
                    ret['ret'] = 'warning'
                    ret['msg'] = '실패하였습니다.'
        elif command == 'queue_command':
            if arg1 == 'cancel':
                queue_item = ModelTvingProgram.get_by_id_in_queue(arg2)
                if queue_item is None:
                    pass
                elif queue_item.is_drm == False:
                    SupportFfmpeg.stop_by_callback_id(f"Tving_program_{arg2}")
                else:
                    # TvingDownloader 는 stop_flag 로 중단 (get_list 미사용)
                    queue_item.cancel = True
            elif arg1 == 'reset':
                if self.download_queue is not None:
                    with self.download_queue.mutex:
                        self.download_queue.queue.clear()
                for _ in ModelTvingProgram.queue_list:
                    if _.is_drm == False:
                        if _.completed == False and _.contents_json != None:
                            SupportFfmpeg.stop_by_callback_id(f"Tving_program_{_.id}")
                ModelTvingProgram.queue_list = []
            elif arg1 == 'delete_completed':
                new = []
                for _ in ModelTvingProgram.queue_list:
                    if _.completed == False:
                        new.append(_)
                ModelTvingProgram.queue_list = new
        return jsonify(ret)


    def plugin_load(self):
        if self.download_queue is None:
            self.download_queue = queue.Queue()

        if self.download_thread is None:
            self.download_thread = threading.Thread(target=self.download_thread_function, args=())
            self.download_thread.daemon = True
            self.download_thread.start()

        if P.ModelSetting.get_bool(f"{self.name}_failed_redownload"):
            self.retry_download_failed()


    def download_thread_function(self):
        while True:
            try:
                while True:
                    if self.current_ffmpeg_count < P.ModelSetting.get_int(f"{self.name}_ffmpeg_max_count"):
                        break
                    time.sleep(5)

                db_item = self.download_queue.get()
                if db_item.cancel:
                    self.download_queue.task_done()
                    continue
                if db_item is None:
                    self.download_queue.task_done()
                    continue
                if db_item.contents_json == None:
                    contents_json = SupportTving.get_info(db_item.episode_code, db_item.quality)
                    db_item.set_contents_json(contents_json)

                count = 0
                if db_item.contents_json['drm'] == False:
                    action = 'hls'
                    db_item.is_drm = False
                else:
                    action = "dash"
                    db_item.is_drm = True
                while True:
                    count += 1
                    streaming_data = SupportTving.get_info(db_item.episode_code, db_item.quality)
                    if streaming_data == None:
                        time.sleep(20)
                        if count > 3:
                            db_item.ffmpeg_status_kor = 'URL실패'
                            break
                    else:
                        db_item.filename = SupportTving.get_filename(db_item.contents_json)
                        break

                if streaming_data is None:
                    self.download_queue.task_done()
                    continue

                # ── TvingDownloader 로 다운로드 ──────────────────────────
                play_info = streaming_data.get('play_info') or {}
                subtitles = []
                try:
                    subtitles = streaming_data['stream']['subtitles'] or []
                except Exception:
                    pass

                downloader = TvingDownloader({
                    'callback_id':        f"{P.package_name}_{self.name}_{db_item.id}",
                    'mpd_url':            streaming_data['url'],
                    'streaming_protocol': "dash" if 'm3u8' not in streaming_data['url'] else "hls",
                    'drm':                streaming_data['drm'],
                    'license_url':        play_info.get('drm_license_uri'),
                    'license_headers':    play_info.get('drm_key_request_properties') or {},
                    'mpd_headers':        play_info.get('mpd_headers') or {},
                    'output_filename':    db_item.filename,
                    'quality':            db_item.quality,
                    'folder_tmp':         os.path.join(F.config['path_data'], 'tmp'),
                    'folder_output':      ToolUtil.make_path(P.ModelSetting.get(f"{self.name}_save_path")),
                    'subtitles':          subtitles,
                    'subtitle_langs':     P.ModelSetting.get_list(f"{self.name}_subtitle_langs", ',') or ['ko'],
                    'clean':              True,
                }, callback_function=self.wvtool_callback_function)
                downloader.start()

                self.current_ffmpeg_count += 1
                self.download_queue.task_done()

            except Exception as e:
                logger.error(f"Exception:{str(e)}")
                logger.error(traceback.format_exc())

    def db_delete(self, day):
        return ModelTvingProgram.delete_all(day=day)

    def retry_download_failed(self):
        failed_list = ModelTvingProgram.get_failed()
        for item in failed_list:
            item.init_for_queue()
            self.download_queue.put(item)
        return len(failed_list)


    def ffmpeg_listener(self, **arg):
        if arg['type'] == 'last':
            self.current_ffmpeg_count += -1

        db_item = ModelTvingProgram.get_by_id_in_queue(arg['callback_id'].split('_')[-1])
        if db_item is None:
            return
        db_item.ffmpeg_arg = arg
        db_item.ffmpeg_status = int(arg['status'])
        db_item.ffmpeg_status_kor = str(arg['status'])
        db_item.ffmpeg_percent = arg['data']['percent']

        db_item.is_downloading = True
        ### edit by lapis
        if int(arg['status']) == 7 or \
           arg['data']['percent'] == 100 or \
           str(arg['status']) in ['완료']:
                db_item.completed = True
                db_item.completed_time = datetime.now()
                db_item.save()
        if arg['type'] == 'last':
            db_item.is_downloading = False

        self.socketio_callback('status', db_item.as_dict_for_queue())



    def wvtool_callback_function(self, args):
        """TvingDownloader 콜백. args = {'status': ..., 'data': {'callback_id': ...}}"""
        db_item = ModelTvingProgram.get_by_id_in_queue(args['data']['callback_id'].split('_')[-1])

        if db_item is None:
            return

        db_item.is_downloading = True
        status = args['status']
        is_last = True

        if status in ("READY", "SEGMENT_FAIL"):
            is_last = False
        elif status == "DOWNLOADING":
            is_last = False
            db_item.is_downloading = True
            db_item.ffmpeg_status_kor = "다운로드중"
        elif status == "DECRYPTING":
            is_last = False
            db_item.ffmpeg_status_kor = "복호화중"
        elif status == "MERGING":
            is_last = False
            db_item.ffmpeg_status_kor = "머지중"
        elif status == "EXIST_OUTPUT_FILEPATH":
            db_item.ffmpeg_status_kor = f"파일 이미 존재"
        elif status == "USER_STOP":
            db_item.ffmpeg_status_kor = "사용자 중지"
        elif status == "COMPLETED":
            db_item.ffmpeg_status_kor = "다운로드 완료"
        elif status == "ERROR":
            db_item.ffmpeg_status_kor = "오류"

        if is_last:
            self.current_ffmpeg_count = max(0, self.current_ffmpeg_count - 1)
            db_item.is_downloading = False
            db_item.completed = True
            db_item.completed_time = datetime.now()
            db_item.save()

        self.socketio_callback('status', db_item.as_dict_for_queue())
































class ModelTvingProgram(ModelBase):
    P = P
    __tablename__ = f'{P.package_name}_program'
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time    = db.Column(db.DateTime)
    completed_time  = db.Column(db.DateTime)
    contents_json = db.Column(db.JSON)
    episode_code    = db.Column(db.String)
    program_id    = db.Column(db.String)
    quality         = db.Column(db.String)
    program_title   = db.Column(db.String)
    episode_number  = db.Column(db.String)
    thumbnail       = db.Column(db.String)
    programimage    = db.Column(db.String)
    completed       = db.Column(db.Boolean)
    current_queue_id = 1
    queue_list = []

    def __init__(self, episode_code, quality):
        self.episode_code   = episode_code
        self.quality        = quality
        self.completed      = False
        self.created_time = datetime.now()


    def init_for_queue(self):
        self.queue_id = self.current_queue_id
        self.current_queue_id += 1
        self.ffmpeg_status = -1
        self.ffmpeg_status_kor = '대기중'
        self.ffmpeg_percent = 0
        self.queue_created_time = datetime.now().strftime('%m-%d %H:%M:%S')
        self.ffmpeg_data = None
        self.cancel = False
        self.is_drm = False
        self.is_downloading = False
        self.filename = None
        self.queue_list.append(self)


    @classmethod
    def get(cls, episode_code, quality):
        with F.app.app_context():
            return db.session.query(ModelTvingProgram).filter_by(
                episode_code=episode_code,
                quality=quality
            ).order_by(desc(cls.id)).first()




    @classmethod
    def is_duplicate(cls, episode_code, quality):
        return (cls.get(episode_code, quality) != None)



    def set_contents_json(self, data):

        self.contents_json = data
        self.program_id = data['content']['program_code']
        self.program_title = data['content']['title']
        self.episode_number = data['content']['frequency']
        self.episode_code = data['content']['code']
        self.thumbnail = data['content']['episode_image'][0]['url'] if data['content']['episode_image'] else data['content']['image'][0]['url']
        self.programimage = data['content']['image'][0]['url']
        self.drm = False if data['stream']['drm_yn'] != "Y" else True
        self.save()


    # 오버라이딩
    @classmethod
    def make_query(cls, req, order='desc', search='', option1='all', option2='all'):
        with F.app.app_context():
            query = F.db.session.query(cls)
            query = cls.make_query_search(query, search, cls.program_title)

            if option1 == 'completed':
                query = query.filter_by(completed=True)
            elif option1 == 'failed':
                query = query.filter_by(completed=False)

            if order == 'desc':
                query = query.order_by(desc(cls.id))
            else:
                query = query.order_by(cls.id)
            return query


    @classmethod
    def remove_all(cls, is_completed=True): # to remove_all(True/False)
        with F.app.app_context():
            count = db.session.query(cls).filter_by(completed=is_completed).delete()
            db.session.commit()
            return count

    @classmethod
    def get_failed(cls):
        with F.app.app_context():
            return db.session.query(ModelTvingProgram).filter_by(
                completed=False
            ).all()



    ### only for queue
    @classmethod
    def get_by_id_in_queue(cls, id):
        for _ in cls.queue_list:
            if _.id == int(id):
                return _

    def as_dict_for_queue(self):
        ret = super().as_dict()
        ret['queue_id'] = self.queue_id
        ret['ffmpeg_status'] = self.ffmpeg_status
        ret['ffmpeg_status_kor'] = self.ffmpeg_status_kor
        ret['ffmpeg_percent'] = self.ffmpeg_percent
        ret['queue_created_time'] = self.queue_created_time
        ret['contents_json'] = self.contents_json
        ret['ffmpeg_data'] = self.ffmpeg_data
        ret['cancel'] = self.cancel
        ret['is_downloading'] = self.is_downloading
        return ret






