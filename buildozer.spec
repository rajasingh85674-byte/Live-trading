[app]
title = Tactical Neural Matrix
package.name = tacticalmatrix
package.domain = org.tnm
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = tests,bin,.git,.github,.buildozer,venv,__pycache__
version = 1.0.0

requirements = python3,kivy==2.3.0,requests==2.31.0,urllib3==1.26.18,idna,charset-normalizer,certifi,pyjnius,android

orientation = portrait
fullscreen = 0

# ---- Android ----
android.permissions = INTERNET,RECORD_AUDIO
android.api = 31
android.minapi = 21
android.ndk = 25b
android.ndk_api = 21
android.archs = arm64-v8a
android.accept_sdk_license = True
android.enable_androidx = True
android.allow_backup = False
android.debug_artifact = apk
android.logcat_filters = *:S python:D

# Filled in by the GitHub workflow (sed). Leave empty.
android.sdk_path =
android.ndk_path =

# Pinned python-for-android release (stable with Kivy 2.3.0 / NDK 25b)
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
