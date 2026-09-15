import os, shutil, subprocess, tempfile
from pathlib import Path

project=Path(__file__).resolve().parents[1]
for old_name, data_name, failure in (
    (old_name, data_name, failure)
    for old_name, data_name in (('SoriNote', 'SoriNote'), ('SoriTaker', 'SoriTaker'), ('SoriTaker', 'SoriNote'))
    for failure in ('none', 'build', 'publish')
):
    with tempfile.TemporaryDirectory(prefix='soritaker-update-') as temp:
        root=Path(temp)
        package=root/'package';package.mkdir()
        shutil.copytree(project/'src',package/'src')
        shutil.copytree(project/'assets',package/'assets')
        for name in ('requirements.txt','requirements-macos.lock','SoriTaker.spec','entitlements.plist','THIRD_PARTY_NOTICES.md'):
            shutil.copy2(project/name,package/name)
        script=(project/'Install.command').read_text()
        script=script.replace('$HOME/Library/Application Support', '$SORITAKER_UPDATE_TEST_ROOT/support')
        script=script.replace('$HOME/Applications','$SORITAKER_UPDATE_TEST_ROOT/Applications')
        for name in ('xcode-select','pgrep','caffeinate','ditto'):
            script=script.replace('/usr/bin/'+name,'test-'+name)
        (package/'Install.command').write_text(script)
        binpath=root/'bin';binpath.mkdir()
        commands={
            'uname':'if [[ "$1" == "-s" ]]; then echo Darwin; else echo arm64; fi',
            'sw_vers':'echo 14.6', 'test-xcode-select':'exit 0', 'test-pgrep':'exit 1',
            'test-caffeinate':'exit 0', 'test-ditto':'cp -R "$1" "$2"',
            'open':'[[ -z "${HF_HUB_OFFLINE+x}" && -z "${TRANSFORMERS_OFFLINE+x}" ]]',
            'curl':'echo UNEXPECTED_NETWORK >&2; exit 90',
            'mv':'if [[ "${SORITAKER_TEST_FAILURE:-}" == publish && "$1" == *SoriTaker-new-*.app ]]; then exit 8; fi; exec /bin/mv "$@"',
        }
        for name,body in commands.items():
            path=binpath/name;path.write_text('#!/bin/bash\n'+body+'\n');path.chmod(0o755)
        data=root/'support'/data_name
        install=data/'installation'
        python=install/'venv'/'bin'/'python';python.parent.mkdir(parents=True)
        python.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
root=pathlib.Path(os.environ['SORITAKER_UPDATE_TEST_ROOT'])
args=sys.argv[1:]
if args[:2]==['-m','PyInstaller']:
    if os.environ.get('SORITAKER_TEST_FAILURE')=='build': sys.exit(7)
    built=root/'support'/os.environ['SORITAKER_TEST_DATA_NAME']/'installation/dist/SoriTaker.app'
    exe=built/'Contents/MacOS/SoriTaker';exe.parent.mkdir(parents=True)
    exe.write_text('#!/usr/bin/env python3\\nimport json,sys\\njson.dump(dict(ok=True),open(sys.argv[2],"w"))\\n')
    exe.chmod(0o755)
    (built/'version').write_text('new')
elif args and args[0]=='-c':
    if args[1].startswith('import PyInstaller'): sys.exit(0)
    os.execv(sys.executable,[sys.executable]+args)
else:
    print('Unexpected installer action',args);sys.exit(91)
''')
        python.chmod(0o755)
        app=root/'Applications'/(old_name+'.app');app.mkdir(parents=True)
        target=app.parent/'SoriTaker.app'
        (app/'version').write_text('old')
        (data/'models').mkdir();(data/'models/keep').write_text('model')
        (data/'recordings').mkdir();(data/'recordings/keep.wav').write_bytes(b'audio')
        env=dict(os.environ, SORITAKER_UPDATE_TEST_ROOT=str(root), SORITAKER_TEST_FAILURE=failure,
                 SORITAKER_TEST_DATA_NAME=data_name, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        env['PATH']=str(binpath)+os.pathsep+env['PATH']
        result=subprocess.run(['bash',str(package/'Install.command'),'--update-only'],env=env,capture_output=True,text=True,timeout=30)
        if failure != 'none':
            assert result.returncode=={'build':7, 'publish':8}[failure],result.stdout+result.stderr
            assert (app/'version').read_text()=='old'
        else:
            assert result.returncode==0,result.stdout+result.stderr
            assert (target/'version').read_text()=='new'
            if old_name != 'SoriTaker':
                assert not app.exists()
            backups=list(app.parent.glob(old_name+'-backup-*.app'))
            assert len(backups)==1 and (backups[0]/'version').read_text()=='old'
        assert (data/'models/keep').read_text()=='model'
        assert (data/'recordings/keep.wav').read_bytes()==b'audio'
        for name in ('SoriTaker.png', 'SoriTaker.icns'):
            assert (install/'source'/'assets'/name).read_bytes()==(project/'assets'/name).read_bytes()
        assert 'UNEXPECTED_NETWORK' not in result.stdout+result.stderr
        assert not (root/'support'/'SoriTaker').exists() if data_name=='SoriNote' else True
        print(f'{old_name}, data={data_name}, failure={failure}: PASS (simulated macOS build commands)')
