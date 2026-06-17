from support.expand.ffmpeg import SupportFfmpeg
from tool import ToolUtil

from support_site import SupportTving

from .setup import *
from .downloader import TvingDownloader

name = 'basic'


class ModuleBasic(PluginModuleBase):
    download_headers = {
        'User-Agent' : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36',
    }

    def __init__(self, P):
        super(ModuleBasic, self).__init__(P, 'setting')
        self.name = name
        self.db_default = {
            f"{self.name}_db_version": "1",
            f"{self.name}_quality": "1080p",
            f"{self.name}_subtitle_langs": "ko",
            f"{self.name}_save_path": "{PATH_DATA}"+os.sep+"download",
            f"{self.name}_recent_code": "",
            f"{self.name}_drm": "RE",
        }
        self.last_data = None

    def process_menu(self, page_name, req):
        arg = P.ModelSetting.to_dict()
        if page_name == 'download':
            arg["code"] = request.args.get('code')
            if arg['code'] is None:
                arg['code'] = P.ModelSetting.get(f"{self.name}_recent_code")
        return render_template(f'{P.package_name}_{name}_{page_name}.html', arg=arg)



    def process_command(self, command, arg1, arg2, arg3, req):
        ret = {'ret':'success'}
        if command == 'analyze':
            if arg2 == '':
                ret = self.analyze(arg1)
            else:
                ret = self.analyze(arg1, quality=arg2)
        elif command == 'download_start':
            data = self.last_data['data']
            play_info = data.get('play_info') or {}
            subtitles = []
            try:
                subtitles = data['stream']['subtitles'] or []
            except Exception:
                pass

            downloader = TvingDownloader({
                'callback_id':        'tving_basic',
                'mpd_url':            data.get('url') or play_info.get('uri', ''),
                'streaming_protocol': self.last_data['available']['action'],
                'drm':                data.get('drm', False),
                'license_url':        play_info.get('drm_license_uri'),
                'license_headers':    play_info.get('drm_key_request_properties') or {},
                'mpd_headers':        play_info.get('mpd_headers') or {},
                'output_filename':    data.get('filename', 'output.mkv'),
                'quality':            P.ModelSetting.get(f"{self.name}_quality"),
                'folder_tmp':         os.path.join(F.config['path_data'], 'tmp'),
                'folder_output':      ToolUtil.make_path(P.ModelSetting.get(f"{self.name}_save_path")),
                'subtitles':          subtitles,
                'subtitle_langs':     P.ModelSetting.get_list(f"{self.name}_subtitle_langs", ',') or ['ko'],
                'clean':              False,
            })
            downloader.start()


        elif command == 'program_page':
            data = SupportTving.get_vod_list(arg1, page=int(arg2))
            ret =  {'url_type': 'program', 'page':arg2, 'code':arg1, 'data' : data}
        return jsonify(ret)


    def analyze(self, url, quality=None):
        try:
            url_type, code = self.parse_url(url)
            P.logger.debug('Analyze %s %s', url_type, code)
            
            if url_type is None:
                return {'url_type': 'None'}
            
            if url_type in ['episode', 'movie']:
                return self.handle_media_content(url_type, code, quality)
            elif url_type == 'program':
                return self.handle_program_content(code)
        except Exception as e:
            P.logger.error(f"Exception: {str(e)}")
            P.logger.error(traceback.format_exc())

    def parse_url(self, url):
        if url.startswith('http'):
            match = re.compile(r'(player|contents)/(.*?)(\&|$|\#)').search(url)
            if match:
                code = match.group(2)
                if code.startswith('E'):
                    return 'episode', code
                elif code.startswith('P'):
                    return 'program', code
                elif code.startswith('M'):
                    return 'movie', code
        else:
            if url.startswith('E'):
                return 'episode', url.strip()
            elif url.startswith('P'):
                return 'program', url.strip()
            elif url.startswith('M'):
                return 'movie', url.strip()
            
        return None, None

    def handle_media_content(self, url_type, code, quality=None):
        if quality is None:
            quality = P.ModelSetting.get(f"{self.name}_quality")
        data = SupportTving.get_info(code, quality)
        action = "dash" if not 'm3u8' in data['url'] else "hls"
        data3 = {
            'preview': ('preview' in data['url'].lower()),
            'current_quality': quality,
            'action': action
        }
        P.ModelSetting.set(f"{self.name}_recent_code", code)
        self.last_data = {'url_type': url_type, 'code': code, 'data': data, 'available': data3}
        return self.last_data
    
    def handle_program_content(self, code):
        data = SupportTving.get_vod_list(code)
        P.ModelSetting.set(f"{self.name}_recent_code", code)
        return {'url_type': 'program', 'page': '1', 'code': code, 'data': data}

