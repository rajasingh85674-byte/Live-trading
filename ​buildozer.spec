[app]
title = Tactical Neural Matrix
package.name = tacticalneuralmatrix
package.domain = org.tnm
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = bin, tests, venv, .git, .github, .buildozer
version = 1.2.0

# python3 comes from the pinned python-for-android release below (Python 3.10.x).
# charset-normalizer 2.1.1 and chardet 5.2.0 are PURE PYTHON on purpose: newer versions ship compiled
# (.so) code, and python-for-android would copy the PC (x86_64) version into the phone app.
requirements = python3,kivy==2.3.0,requests==2.31.0,urllib3==1.26.18,idna==3.7,certifi,charset-normalizer==2.1.1,chardet==5.2.0,pyjnius,android

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,RECORD_AUDIO
android.api = 33
android.minapi = 24
android.ndk = 25b
android.ndk_api = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False
android.wakelock = False
android.presplash_color = #0D0906
android.logcat_filters = *:S python:D
# filled in by the GitHub workflow (sed) so no path mismatch is possible
android.sdk_path =
android.ndk_path =

p4a.branch = v2023.09.16
p4a.bootstrap = sdl2

[buildozer]
log_level = 2
warn_on_root = 0
bin_dir = ./bin
build_dir = ./.buildozer
