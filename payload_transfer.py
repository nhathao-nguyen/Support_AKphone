"""Publish a complete payload, then remove its originals from the source inventory."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import uuid

TRANSFER_LOCK = threading.RLock()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def recover_pending(running_root, source_root):
    """Restore source files after a process stops in the middle of source cleanup."""
    inventory = Path(source_root).resolve()
    store_root = Path(running_root).resolve() / '.payload-store'
    recovered = 0
    if not store_root.is_dir():
        return recovered
    with TRANSFER_LOCK:
        for manifest_path in store_root.glob('*/*/manifest.json'):
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if manifest.get('state') != 'published':
                continue
            for key,value in manifest['source'].items():
                source = Path(value).resolve()
                raw = Path(manifest['raw'][key]).resolve()
                if inventory not in source.parents or manifest_path.parent not in raw.parents:
                    raise ValueError('Manifest chuyển bài có đường dẫn ngoài phạm vi kho.')
                if source.exists():
                    continue
                if digest(raw) != manifest['sha256'][key]:
                    raise ValueError('Bản nguồn lưu không khớp; cần kiểm tra chuyển bài gián đoạn.')
                source.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(raw,source)
                recovered += 1
            manifest['state'] = 'interrupted_source_restored'
            manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return recovered


def transfer_payload(post, video_dir, caption_destinations, comment_destinations,
                     page_name, position):
    """Keep exact originals in .payload-store; remove source only after verified publish.

    Cross-volume cut uses copy + SHA256 verification + source removal. Raw caption
    and comment are retained because the AKPhone outputs flatten their newlines.
    Failed publishing/deletion rolls back outputs and restores any removed source.
    """
    with TRANSFER_LOCK:
        directory = Path(video_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        sources = {key: Path(post[key]).resolve() for key in ('video_file','caption_file','comment_file') if post.get(key)}
        if 'video_file' not in sources or any(not p.is_file() for p in sources.values()):
            raise ValueError('Thiếu file nguồn; chưa chuyển bài khỏi kho.')
        if position < 1:
            raise ValueError('Số bài phải lớn hơn 0.')
        captions = [Path(p).resolve() for p in caption_destinations if p]
        comments = [Path(p).resolve() for p in comment_destinations if p]
        if not captions or not comments:
            raise ValueError('Thiếu đường caption/comment đích; chưa chuyển bài khỏi kho.')
        targets = [directory / sources['video_file'].name] + captions + comments
        if len(set(targets)) != len(targets):
            raise ValueError('Các file đích bị trùng đường dẫn.')
        if any(p == directory or directory in p.parents for p in sources.values()):
            raise ValueError('Kho nguồn chồng lên thư mục video đang chạy.')
        if any(p in sources.values() for p in targets):
            raise ValueError('File đích trùng nguồn; không thể cắt bài an toàn.')
        if any(p.parent == directory for p in captions + comments):
            raise ValueError('Caption/comment không được nằm trong folder video đang chạy.')
        hashes = {key: digest(path) for key,path in sources.items()}
        reused = bool(post.get('_payload_archive'))
        store = Path(post['_payload_archive']) if reused else directory.parent / '.payload-store' / directory.name / uuid.uuid4().hex
        store.mkdir(parents=True, exist_ok=True)
        raw = sources if reused else {key: store / key / sources[key].name for key in sources}
        manifest_path = store / 'manifest.json'
        manifest = {'source':{k:str(v) for k,v in sources.items()},'raw':{k:str(v) for k,v in raw.items()},
                    'sha256':hashes,'targets':[str(p) for p in targets], 'state':'preparing'}
        def save_state(state):
            if reused:
                return
            manifest['state'] = state
            temporary = manifest_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(temporary,manifest_path)
        save_state('preparing')
        if not reused:
            for key, path in sources.items():
                raw[key].parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,raw[key])
                if digest(raw[key]) != hashes[key]:
                    raise ValueError('Bản chuyển không khớp nguồn; giữ nguyên kho bài.')
        caption = ' '.join(line.strip() for line in raw['caption_file'].read_text(encoding='utf-8').splitlines() if line.strip()) if 'caption_file' in raw else f'Bài đăng #{position} cho {page_name}'
        comment = ' '.join(line.strip() for line in raw['comment_file'].read_text(encoding='utf-8').splitlines() if line.strip()) if 'comment_file' in raw else f'Bình luận #{position} cho {page_name}'
        work_parent = directory.parent / '.transfer-work'
        work_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work_parent) as temporary:
            work = Path(temporary)
            previous = list(directory.iterdir())
            previous = [p for p in previous if p.is_file()]
            existing = list(dict.fromkeys(previous + [p for p in captions+comments if p.exists()]))
            backups, installed, removed = {}, [], []
            try:
                for number,path in enumerate(existing):
                    backup = work / f'old-{number}'
                    shutil.copy2(path,backup)
                    backups[path] = backup
                staged_video = work / 'new-video'
                shutil.copy2(raw['video_file'],staged_video)
                staged = [(targets[0],staged_video,hashes['video_file'])]
                for number,(path,text) in enumerate([(p,caption) for p in captions]+[(p,comment) for p in comments]):
                    staged_text = work / f'new-text-{number}'
                    staged_text.write_text(text,encoding='utf-8')
                    staged.append((path,staged_text,digest(staged_text)))
                for path,staged_file,expected in staged:
                    path.parent.mkdir(parents=True,exist_ok=True)
                    # A sibling temp file makes replace atomic even across volumes.
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.payload-',delete=False) as f:
                        sibling = Path(f.name)
                    try:
                        shutil.copy2(staged_file,sibling)
                        installed.append(path)
                        os.replace(sibling,path)
                    finally:
                        sibling.unlink(missing_ok=True)
                    if digest(path) != expected:
                        raise ValueError('File đích không khớp; giữ nguyên nguồn.')
                for old in previous:
                    if old != targets[0]:
                        old.unlink()
                save_state('published')
                if not reused:
                    # Check all originals again before cutting any of them.
                    if any(digest(sources[key]) != hashes[key] for key in sources):
                        raise ValueError('Nguồn thay đổi khi chuyển bài; giữ nguyên kho.')
                    for key,path in sources.items():
                        path.unlink()
                        removed.append(key)
                    save_state('complete')
                post.update({key:str(path) for key,path in raw.items()})
                post['_payload_archive'] = str(store)
            except Exception:
                for key in removed:
                    if not sources[key].exists():
                        shutil.copy2(raw[key],sources[key])
                for path in installed:
                    if path not in backups:
                        path.unlink(missing_ok=True)
                for path,backup in backups.items():
                    shutil.copy2(backup,path)
                save_state('failed_source_retained')
                raise
        return str(store)
