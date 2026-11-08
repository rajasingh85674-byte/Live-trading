[app]

# (str) Title of your application
title = Tactical Neural Matrix

# (str) Package name
package.name = tacticalmatrix

# (str) Package domain (needed for android packaging)
package.domain = org.tnm

# (list) Source files to include (let it empty to include all files)
source.include_exts = py,png,jpg,kv,atlas,json

# (list) Source files to exclude (let it empty to exclude nothing)
source.exclude_dirs = tests,bin,.git,.github,.buildozer,.venv,__pycache__

# (str) Application versioning
version = 1.0.0

# (list) Application requirements
# Add all necessary modules for Kivy, requests, and Android bridge
requirements = python3,kivy==2.3.0,requests==2.31.0,urllib3==1.26.18,idna,charset-normalizer,certifi,pyjnius,android

# (str) Custom source folders for python modules
#source.main_ext = py

# (str) Display orientation
orientation = portrait

# (list) The orientation supported for android
#orientation = portrait, landscape

# (bool) Indicate if the application should be fullscreen or not
fullscreen = 0

# (string) Presplash background color
#presplash.color = #010409

# (list) Permissions
android.permissions = INTERNET,RECORD_AUDIO

# (int) Target Android API, should be as high as possible.
android.api = 33

# (int) Minimum API your APK will support.
android.minapi = 24

# (int) Android NDK version to use
android.ndk = 25b

# (int) Android NDK API to use. This is the minimum API version.
android.ndk_api = 24

# (list) Android archs to build for, can be: arm64-v8a, armeabi-v7a, x86, x86_64
android.archs = arm64-v8a

# (bool) Enable Android auto backup
android.allow_backup = False

# (str) The format used to package the app for release/debug (apk or aab)
android.debug_artifact = apk

[buildozer]

# (int) Log level (0 = error only, 1 = info, 2 = debug (with command output))
log_level = 2

# (int) Display warning if buildozer is run as root (0 = False, 1 = True)
warn_on_root = 1
