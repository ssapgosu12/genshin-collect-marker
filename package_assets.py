from pathlib import Path
import shutil
root=Path(__file__).resolve().parent
target=root/'dist/GenshinCollectMarker'
for relative in ['build-work/capture-hdr.exe','upstream-genshin-overlay/build/Release/genshin-overlay.exe','upstream-genshin-overlay/zoombar_template.png']:
    source=root/relative
    if not source.is_file():raise FileNotFoundError(source)
    destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
for relative in ['sounds','upstream-genshin-overlay/build/Release/map_data','THIRD_PARTY_LICENSES']:
    shutil.copytree(root/relative,target/relative,dirs_exist_ok=True,ignore=shutil.ignore_patterns('.playback','map-index.bin.gz','published-index.json'))
for name in ['README.md','LICENSE','THIRD_PARTY_NOTICES.md']:shutil.copy2(root/name,target/name)
print(target)
