# -*- coding: utf-8 -*-
"""下载「语音输入」所需的本地模型（sherpa-onnx）。

用法：
    .venv\\Scripts\\python.exe scripts\\fetch_speech_model.py [模型名]
    模型名：streaming（默认，实时流式识别）| offline（离线整段识别）| denoise（降噪模型）

下载后模型落在 data/models/（data/ 已 gitignore，不会进仓库）。
网络受限时可先设代理，例如：
    $env:HTTPS_PROXY="http://127.0.0.1:7897"
"""
import os
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(ROOT, "data", "models")

_ASR_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
_ENH_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speech-enhancement-models/"

# 压缩包模型：name 为目录名，files 为就绪判断需要的文件
ARCHIVES = {
    # 实时流式中文（int8，CPU 可实时）——语音输入默认使用
    "streaming": {
        "name": "sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30",
        "files": ["encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt"],
    },
    # 离线整段中文（中英 int8，体积小）
    "offline": {
        "name": "sherpa-onnx-paraformer-zh-small-2024-03-09",
        "files": ["tokens.txt"],
    },
    # 两遍识别的「精修」离线模型（SenseVoice：中英日韩粤，准确率高）
    "final": {
        "name": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        "files": ["tokens.txt"],
    },
}
# 单文件模型（直接放 data/models/ 下）
FILES = {
    # 流式降噪（DPDFNet baseline，8.4MB）
    "denoise": {"name": "dpdfnet_baseline.onnx", "url": _ENH_RELEASE + "dpdfnet_baseline.onnx"},
}
DEFAULT_KEY = "streaming"


def _progress(count, block_size, total_size):
    if total_size <= 0:
        return
    done = min(count * block_size, total_size)
    mb = done / 1024.0 / 1024.0
    total_mb = total_size / 1024.0 / 1024.0
    sys.stdout.write("\r  下载中… %5.1f%%  (%.1f / %.1f MB)" % (done * 100.0 / total_size, mb, total_mb))
    sys.stdout.flush()


def _download(url, dest_path):
    try:
        urllib.request.urlretrieve(url, dest_path, _progress)
        return True
    except Exception as e:
        print("\n下载失败：%s" % e)
        print("若在国内网络，可先设置代理后重试：")
        print('  $env:HTTPS_PROXY="http://127.0.0.1:7897"')
        return False


def fetch_archive(item):
    name = item["name"]
    target = os.path.join(MODELS_DIR, name)
    if all(os.path.isfile(os.path.join(target, f)) for f in item["files"]):
        print("模型已存在，跳过下载：%s" % target)
        return 0
    url = _ASR_RELEASE + name + ".tar.bz2"
    os.makedirs(MODELS_DIR, exist_ok=True)
    tar_path = os.path.join(MODELS_DIR, name + ".tar.bz2")
    print("准备下载模型：%s\n  来源：%s" % (name, url))
    if not _download(url, tar_path):
        return 1
    print("\n  下载完成，正在解压…")
    try:
        with tarfile.open(tar_path, "r:bz2") as tf:
            try:
                tf.extractall(MODELS_DIR, filter="data")   # Python 3.12+：安全解压
            except TypeError:
                tf.extractall(MODELS_DIR)
    except Exception as e:
        print("解压失败：%s" % e)
        return 1
    finally:
        try:
            os.remove(tar_path)
        except OSError:
            pass
    missing = [f for f in item["files"] if not os.path.isfile(os.path.join(target, f))]
    if missing:
        print("解压后缺少文件 %s，请检查：%s" % (missing, target))
        return 1
    print("完成！模型已就绪：%s" % target)
    return 0


def fetch_file(item):
    name = item["name"]
    target = os.path.join(MODELS_DIR, name)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        print("模型已存在，跳过下载：%s" % target)
        return 0
    os.makedirs(MODELS_DIR, exist_ok=True)
    print("准备下载模型：%s\n  来源：%s" % (name, item["url"]))
    if not _download(item["url"], target):
        return 1
    print("\n完成！模型已就绪：%s" % target)
    return 0


def fetch(key):
    if key in ARCHIVES:
        return fetch_archive(ARCHIVES[key])
    if key in FILES:
        return fetch_file(FILES[key])
    print("未知模型：%s（可选：%s）" % (key, "、".join(list(ARCHIVES) + list(FILES))))
    return 1


def main():
    key = (sys.argv[1].strip().lower() if len(sys.argv) > 1 else DEFAULT_KEY)
    return fetch(key)


if __name__ == "__main__":
    sys.exit(main())
