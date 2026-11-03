# Face models (bundled into the installer, NOT committed)

| File | Size | Licence | Source |
| --- | --- | --- | --- |
| face_detection_yunet_2023mar.onnx (YuNet detector) | 232,589 B | MIT | https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet |
| face_recognition_sface_2021dec.onnx (SFace recognizer) | 38,696,353 B | Apache-2.0 | https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface |

Download both into this folder before building (`build_release.ps1` / PyInstaller bundles them as `models/`):
`curl -L -o models/<file> https://github.com/opencv/opencv_zoo/raw/main/models/<dir>/<file>`.
Runtime libraries: opencv-python-headless 5.x (Apache-2.0, ~44 MB wheel), numpy 2.x (BSD-3-Clause, ~13 MB wheel).
Matching threshold: cosine 0.363 (SFace's published value). **No liveness / anti-spoofing is implemented.**
