setting = {
    'filepath' : __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': None,
    'menu': {
        'uri': __package__,
        'name': '티빙',
        'list': [
            {
                'uri': 'basic',
                'name': '기본',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'download', 'name': '다운로드'},
                ]
            },
            {
                'uri': 'recent',
                'name': '최근방송 자동',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'list', 'name': '목록'},
                ]
            },
            {
                'uri': 'program',
                'name': '프로그램별 자동',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'select', 'name': '선택'},
                    {'uri': 'queue', 'name': '큐'},
                    {'uri': 'list', 'name': '목록'},
                ]
            },
            {
                'uri': 'log',
                'name': '로그',
            },
        ]
    },
    'default_route': 'normal',
}
from plugin import *

DEFINE_DEV = False
if os.path.exists(os.path.join(os.path.dirname(__file__), 'mod_basic.py')):
    DEFINE_DEV = True

P = create_plugin_instance(setting)
try:
    if DEFINE_DEV:
        from .mod_basic import ModuleBasic
        from .mod_program import ModuleProgram
        from .mod_recent import ModuleRecent
    else:
        from support import SupportSC
        ModuleBasic = SupportSC.load_module_P(P, 'mod_basic').ModuleBasic
        ModuleRecent = SupportSC.load_module_P(P, 'mod_recent').ModuleRecent
        ModuleProgram = SupportSC.load_module_P(P, 'mod_program').ModuleProgram
    
    P.set_module_list([ModuleBasic, ModuleRecent, ModuleProgram])
except Exception as e:
    P.logger.error(f'Exception:{str(e)}')
    P.logger.error(traceback.format_exc())
