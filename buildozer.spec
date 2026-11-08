[app]
title = Tactical Neural Matrix
package.name = tacticalmatrix
package.domain = org.tnm
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = tests,bin,.git,.github,.buildozer,.venv,__pycache__
version = 1.0.0
requirements = python3,kivy==2.3.0,requests==2.31.0,urllib3==1.26.18,idna,charset-normalizer,certifi,pyjnius,android
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,RECORD_AUDIO
android.api = 33
android.minapi = 24
android.ndk = 25b
android.ndk_api = 24
android.archs = arm64-v8a
android.allow_backup = False
android.debug_artifact = apk

[buildozer]
log_level = 2
warn_on_root = 1
