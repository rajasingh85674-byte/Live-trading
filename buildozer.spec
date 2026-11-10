[app]

title = Tactical Neural Matrix
package.name = tacticalmatrix
package.domain = org.tnm
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = tests,bin,.git,.github,.buildozer,venv,__pycache__
version = 1.0.0

# main.py के अंदर की इम्पोर्ट्स के मुताबिक सभी ज़रूरी पैकेजेस
requirements = python3,kivy==2.3.0,requests==2.31.0,urllib3==1.26.18,idna,charset-normalizer,certifi,pyjnius,android

orientation = portrait
fullscreen = 0
android.permissions = INTERNET,RECORD_AUDIO,ACCESS_NETWORK_STATE

android.api = 33
android.minapi = 24
android.ndk = 25.1.8937393
android.archs = arm64-v8a

android.sdk_path = /home/runner/android-sdk
android.ndk_path = /home/runner/android-sdk/ndk/25.1.8937393

android.accept_sdk_license = True
android.enable_androidx = True
android.allow_backup = False
android.logcat_filters = *:S python:D

[buildozer]
log_level = 2
warn_on_root = 0
