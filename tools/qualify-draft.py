"""Public orchestration only: all game inputs, logs and results stay in a draft.

Uses only the ephemeral token of THIS release repository. No private-repository
credentials, source checkout, public Actions artifact or cache. Never publishes,
changes a release, removes an asset, or reports failed QA as a qualified build.
"""
import hashlib, json, os, re, shutil, signal, subprocess, sys, time, zipfile
from pathlib import Path

REPO = os.environ['GITHUB_REPOSITORY']
RELEASE = os.environ['DRAFT_RELEASE_ID']
COMMIT = os.environ['SOURCE_COMMIT']
DIGEST = os.environ['CARTRIDGE_SHA256']
NAME = os.environ['CARTRIDGE_NAME']
PLATFORM = os.environ['BUILD_PLATFORM']
SCOPE = os.environ['BUILD_SCOPE']
RUN_ID = os.environ['GITHUB_RUN_ID']
ROOT = Path(os.environ['RUNNER_TEMP']) / ('iron-qualification-' + RUN_ID + '-' + PLATFORM)

def sha(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def api(route):
    result = subprocess.run(['gh', 'api', 'repos/' + REPO + route], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)

def draft():
    record = api('/releases/' + RELEASE)
    if not record.get('draft') or not re.fullmatch(r'qualification-v055-[a-f0-9]{7,40}', record['tag_name']):
        raise ValueError('Qualification requires the original unpublished draft')
    return record

def run(command, cwd, log, seconds=120, checkpoint=None):
    with log.open('ab') as output:
        process = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=os.name != 'nt')
        deadline = time.monotonic() + seconds
        next_checkpoint = time.monotonic() + 180
        checkpoint_number = 0
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError('Owned qualification stage exceeded its deadline')
                if log.stat().st_size > 16_000_000:
                    raise ValueError('Qualification log exceeds its bounded record')
                if shutil.disk_usage(ROOT).free < 8_000_000_000:
                    raise ValueError('Ephemeral runner crossed its 8 GB running reserve')
                if checkpoint is not None and time.monotonic() >= next_checkpoint:
                    checkpoint_number += 1
                    save_progress(checkpoint, checkpoint_number)
                    next_checkpoint = time.monotonic() + 180
                time.sleep(.5)
        except BaseException:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name != 'nt':
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=10)
            raise
    if process.returncode:
        raise RuntimeError('Qualification stage failed; details retained only in the unpublished draft')

def verify_cartridge(cartridge, target):
    if sha(cartridge) != DIGEST:
        raise ValueError('Cartridge digest differs from the creator-frozen input')
    with zipfile.ZipFile(cartridge) as archive:
        entries = archive.infolist()
        if len(entries) > 601 or sum(row.file_size for row in entries) > 80_100_000:
            raise ValueError('Cartridge expansion exceeds the admitted bound')
        names = [row.filename for row in entries]
        if len(set(names)) != len(names):
            raise ValueError('Duplicated cartridge path')
        for row in entries:
            name = row.filename
            if name.startswith('/') or '\\' in name or any(p in ('', '.', '..', '.git') for p in name.split('/')) or ((row.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError('Unsafe cartridge member')
        receipt = json.loads(archive.read('build-source.json'))
        if receipt.get('schema') != 'iron-litany-build-export-v1' or receipt.get('commit') != COMMIT or receipt.get('sourceStatus') != 'clean':
            raise ValueError('Cartridge source identity differs from the frozen private commit')
        expected = {row['path']: row for row in receipt['files']}
        if len(expected) != len(receipt['files']) or set(names) != set(expected) | {'build-source.json'}:
            raise ValueError('Cartridge contains undeclared or missing inputs')
        for name, row in expected.items():
            data = archive.read(name)
            if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                raise ValueError('Cartridge input readback failure')
        archive.extractall(target)

def upload(files, label):
    # Refresh immediately before upload. Assets are never attached to a
    # published release, and failed attempts never overwrite a prior result.
    record = draft()
    command = ['gh', 'release', 'upload', record['tag_name'], '--repo', REPO]
    command += [str(file) + '#' + label for file in files]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError('Draft result upload failed')

def save_progress(checkpoint, number):
    # A finite native deadline permits at most sixteen 3-minute records. Only
    # reported game instruments/automation counters are retained, never source
    # contents or public Actions artifacts. Existing records are immutable.
    if number > 16 or not checkpoint.exists() or checkpoint.stat().st_size > 3_000_000:
        return
    try:
        original = json.loads(checkpoint.read_text())
    except (ValueError, OSError):
        return  # A concurrent checkpoint write can be read on the next interval.
    last = original.get('last', {})
    summary = {'runId': RUN_ID, 'platform': PLATFORM, 'scope': SCOPE,
               'sourceCommit': COMMIT, 'orchestrationCommit': os.environ['GITHUB_SHA'],
               'cartridgeSha256': DIGEST, 'published': False, 'completed': False,
               'elapsedSeconds': original.get('elapsedSeconds'),
               'last': {k: last.get(k) for k in ('phase', 'leg', 'progress', 'hull', 'spirit', 'supplies', 'guidance')},
               'metrics': original.get('metrics'), 'recentTrace': original.get('trace', [])[-5:]}
    data = (json.dumps(summary, indent=2) + '\n').encode()
    if len(data) > 32_000:
        return
    file = checkpoint.parent / ('progress-' + RUN_ID + '-' + PLATFORM + '-' + str(number) + '.json')
    file.write_bytes(data)
    try:
        upload([file], 'Unpublished incomplete native checkpoint')
    except (RuntimeError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        # Optional telemetry cannot turn otherwise correct native input into
        # a failure. The final result still requires its real protected upload.
        print('Protected checkpoint transfer deferred; native qualification continues.', flush=True)
        return
    print('Protected native checkpoint saved; qualification is still incomplete.', flush=True)

def main():
    if REPO != 'redgargoyle/Iron-Litany-Releases' or not re.fullmatch(r'[0-9]+', RELEASE) or not re.fullmatch(r'[a-f0-9]{40}', COMMIT) or not re.fullmatch(r'[a-f0-9]{64}', DIGEST) or not re.fullmatch(r'build-input-[a-f0-9]{40}\.zip', NAME) or PLATFORM not in ('linux', 'win32') or SCOPE not in ('windows-diagnostic', 'full-pair'):
        raise ValueError('Invalid qualification request')
    if SCOPE == 'windows-diagnostic' and PLATFORM != 'win32':
        raise ValueError('Diagnostic scope is Windows only')
    if ROOT.exists():
        raise ValueError('Refusing to reuse a previous qualification workspace')
    ROOT.mkdir()
    source, output, evidence = ROOT / 'source', ROOT / 'desktop', ROOT / 'evidence'
    source.mkdir(); evidence.mkdir()
    record = draft()
    asset = next((a for a in record['assets'] if a['name'] == NAME), None)
    if not asset or asset['size'] > 80_000_000:
        raise ValueError('Expected bounded cartridge is missing')
    run(['gh', 'release', 'download', record['tag_name'], '--repo', REPO, '--pattern', NAME, '--dir', str(ROOT)], ROOT, evidence / 'transfer.log', 180)
    verify_cartridge(ROOT / NAME, source)
    log = evidence / 'checks-and-build.log'
    node = shutil.which('node')
    if not node:
        raise ValueError('Pinned Node runtime unavailable')
    # Invoke the exact package scripts portably; never omit a test file.
    tests = sorted(str(p.relative_to(source)) for p in (source / 'tests').glob('*.test.mjs'))
    run([node, '--test', '--test-concurrency=2', *tests], source, log)
    run([node, 'tools/check.mjs'], source, log)
    print('Every frozen test and runtime reference check passed.', flush=True)
    run([node, 'tools/package-desktop.mjs', '--platform=' + PLATFORM, '--out=' + str(output), '--source-receipt=' + str(source / 'build-source.json')], source, log, 360)
    package = 'windows' if PLATFORM == 'win32' else 'linux'
    bundle = output / ('IronLitany-' + package + '-x64')
    manifest = json.loads((bundle / 'build-manifest.json').read_text())
    if manifest.get('commit') != COMMIT or manifest.get('sourceStatus') != 'clean' or manifest.get('sourceMethod') != 'hash-verified-export':
        raise ValueError('Native bundle lost its original input identity')
    print('Official runtime packaged; original private source identity retained.', flush=True)
    command = [str(bundle / ('IronLitany.exe' if PLATFORM == 'win32' else 'IronLitany')), '--smoke-test', '--campaign-qa', '--smoke-output=' + str(evidence), '--ignore-gpu-blocklist', '--use-gl=angle']
    if PLATFORM == 'win32':
        command += ['--use-angle=swiftshader']
        budget = 3000
    else:
        run(['sudo', 'chown', 'root:root', str(bundle / 'chrome-sandbox')], ROOT, log)
        run(['sudo', 'chmod', '4755', str(bundle / 'chrome-sandbox')], ROOT, log)
        command = ['xvfb-run', '-a', *command, '--use-angle=gl']
        os.environ['LIBGL_ALWAYS_SOFTWARE'] = '1'
        budget = 960
    print('Launching actual native controls and the three-watch campaign.', flush=True)
    run(command, source, evidence / 'native.log', budget, checkpoint=evidence / 'campaign-checkpoint.json')
    report = json.loads((evidence / 'smoke-report.json').read_text())
    if report.get('ok') is not True or report.get('campaignQA', {}).get('ok') is not True:
        raise ValueError('Actual native campaign did not qualify')
    prefix = ('diagnostic' if SCOPE == 'windows-diagnostic' else 'pair') + '-' + RUN_ID + '-'
    receipt = {'schema': 'iron-litany-draft-attempt-v1', 'ok': True, 'sourceCommit': COMMIT,
               'cartridgeSha256': DIGEST, 'sourceMethod': manifest['sourceMethod'],
               'scope': SCOPE, 'platform': PLATFORM, 'runId': RUN_ID,
               'orchestrationCommit': os.environ['GITHUB_SHA'], 'published': False}
    (evidence / 'attempt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    payloads = []
    for suffix in ('.zip', '.zip.sha256', '.zip.manifest.json'):
        file = output / ('IronLitany-' + package + '-x64' + suffix)
        target = output / (prefix + file.name)
        file.rename(target); payloads.append(target)
    archive = ROOT / (prefix + package + '-evidence.zip')
    bounded_evidence(evidence, archive)
    upload([*payloads, archive], 'Unpublished native qualification')
    print('Native ' + package + ' campaign completed; original source identity verified; outputs held unpublished.')

def bounded_evidence(evidence, archive):
    files = [p for p in evidence.rglob('*') if p.is_file()]
    if sum(p.stat().st_size for p in files) > 32_000_000:
        raise ValueError('Evidence exceeds the bounded record')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for p in files:
            out.write(p, str(p.relative_to(evidence)))

if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        # Keep details in the protected draft, never raw game/source exceptions
        # in public logs. Only this runner's child is subject to its deadline.
        evidence = ROOT / 'evidence'
        if evidence.exists():
            (evidence / 'attempt.json').write_text(json.dumps({'ok': False, 'sourceCommit': COMMIT, 'platform': PLATFORM, 'scope': SCOPE, 'runId': RUN_ID, 'error': str(error), 'published': False}, indent=2) + '\n')
            try:
                archive = ROOT / ('failed-' + RUN_ID + '-' + PLATFORM + '-evidence.zip')
                bounded_evidence(evidence, archive)
                upload([archive], 'Unpublished failed qualification evidence')
            except BaseException:
                pass
        print('Native qualification failed; bounded details held in the unpublished draft when available.', file=sys.stderr)
        sys.exit(1)
