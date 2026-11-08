[app]
title = Tactical Neural Matrix
package.name = tacticalneuralmatrix
package.domain = org.tactical
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,xml
version = 1.0.0
requirements = python3,kivy==2.3.0,requests==2.31.0,urllib3==1.26.18,idna,charset-normalizer,certifi,pyjnius,android
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,RECORD_AUDIO
android.minapi = 21
android.api = 31
android.ndk = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True
android.debug_artifact = apk

[buildozer]
log_level = 2
warn_on_build_tools_version = 1
