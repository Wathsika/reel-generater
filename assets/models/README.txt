face_detection_yunet_2023mar.onnx
=================================

Face detector used by pipeline/crop.py to place the 9:16 crop window over the
speaker instead of over the middle of the picture.

Source : https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
Model  : face_detection_yunet_2023mar.onnx  (232,589 bytes)
Paper  : Wu, Peng, Yu - "YuNet: A Tiny Millisecond-level Face Detector",
         Machine Intelligence Research 20(5), 2023
Licence: MIT (see the LICENSE file in that model directory)

Why it is committed here: it is a 230 KB file, so a free runner does not have to
download it on every run, and the crop stage cannot break because an upstream
URL moved. It runs through OpenCV's DNN module - no PyTorch, no HuggingFace
token, no model download.

Attribution is kept in this file and in the README.
