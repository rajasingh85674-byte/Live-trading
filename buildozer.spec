[app]

# (str) Title of your application
title = Tactical Neural Matrix

# (str) Package name
package.name = tacticalmatrix

# (str) Package domain (needed for android packaging)
package.domain = org.tnm

# (str) Source code where the main.py lives
source.dir = .

# (list) Source files to include
source.include_exts = py,png,jpg,kv,atlas,json

# (list) Source files to exclude
source.exclude_dirs = tests,bin,.git,.github,.buildozer,venv,__pycache__

# (str) Application versioning
version = 1.0.0

# (list) Application requirements
requirements = python3,kivy==2.3.0,requests==2.31.0,urllib3==1.26.18,idna,charset-normalizer,certifi,pyjnius,android

# (str) Supported orientation
orientation = portrait

# (list) The gles version to use
fullscreen = 0

# Android permissions
android.permissions = INTERNET,RECORD_AUDIO

# (int) Target Android API
android.api = 33

# (int) Minimum API your APK will support
android.minapi = 24

# (str) Android NDK version to use
android.ndk = 25.1.8937393

# (list) Application architectures to build for
android.archs = arm64-v8a

# (str) Direct paths for GitHub Actions
android.sdk_path = /home/runner/android-sdk
android.ndk_path = /home/runner/android-sdk/ndk/25.1.8937393

# (bool) Enable AndroidX and License acceptance
android.accept_sdk_license = True
android.enable_androidx = True
android.allow_backup = False
android.logcat_filters = *:S python:D

[buildozer]

# (int) Log level (2 = debug mode with full logs)
log_level = 2

# (int) Display warning if buildozer is run as root
warn_on_root = 0
