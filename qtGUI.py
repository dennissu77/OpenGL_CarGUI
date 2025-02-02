import base64
import json
import os
import sys
import time

import cv2
import numpy as np
import yaml
from easing_functions import CubicEaseInOut
from numba import jit
from OpenGL.GL import *
from PyQt5 import QtCore
from PyQt5.QtCore import Qt, QThread, QTimer
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

from ui.threads import DataRecievedThread
from ui.UIPrototype import Ui_MainWindow


# @jit
def process_bev_data(img):
    dot_data = []

    for i in range(0, img.shape[0], 3):
        for j in range(0, img.shape[1], 3):
            pixel = img[i, j]
            x = j / 200
            y = i / 200
            if pixel[0] == 0 and pixel[1] == 0 and pixel[2] == 0:
                dot_data.append({"x": x, "y": y, "cls": 0})
            elif pixel[0] == 255 and pixel[1] == 0 and pixel[2] == 0:
                dot_data.append({"x": x, "y": y, "cls": 1})
            elif pixel[0] == 0 and pixel[1] == 0 and pixel[2] == 255:
                dot_data.append({"x": x, "y": y, "cls": 2})

    return dot_data


class Car_MainWindow(Ui_MainWindow):
    def __init__(self):
        super(Car_MainWindow, self).__init__()
        
        # 載入設定檔 
        with open("config.yaml", "r") as f:
            config = yaml.load(f, Loader=yaml.FullLoader)
            self.draw_mode = config["draw_mode"]
            ip = config["ip"]
            port = config["port"]
            mat_dict = config["mat_dict"]

        
        self.mat_container = self.load_material_img(mat_dict)

        self.cur_frame_data = {}

        self.idx_data = 0

        # 視角上升，下降狀態索引
        self.idx_cam_rise = 0
        self.idx_cam_down = 0

        # 視角上升完成、視角變動鎖定旗標
        self.flag_cam_rised = False
        self.flag_cam_lock = False

        # 視角變化的緩和函數
        self.arr_cam_ease = list(
            map(CubicEaseInOut(start=0, end=25), np.arange(0, 1, 0.1))
        )

        self.queue_inter = []
        self.tl_dire = "left"
        self.isLightOn = False
        self.display_tl = False
        self.flag_frame_changed = False

        # 初始化接收資料線程
        self.thread_data = QThread()
        self.datathread = DataRecievedThread(ip=ip, port=port)
        self.datathread.moveToThread(self.thread_data)
        self.thread_data.started.connect(self.datathread.run)
        self.datathread.data_recieved_signal.connect(self.recv_data)
        self.thread_data.start()

    def turn_light(self):
        """
        顯示方向燈
        """       
        if self.cur_frame_data:
            steering = round(float(self.cur_frame_data["steering"]), 2)
            # 判斷左右轉
            if steering > 60:
                self.display_tl = True
                self.tl_dire = "left"
            elif steering < -60:
                self.display_tl = True
                self.tl_dire = "right"
            else:
                self.display_tl = False

            if self.display_tl:
                if self.isLightOn:
                    self.isLightOn = False
                    if self.tl_dire == "left":
                        self.img_steer_left.setPixmap(self.mat_container["left"])
                    elif self.tl_dire == "right":
                        self.img_steer_right.setPixmap(self.mat_container["right"])
                else:
                    if self.tl_dire == "left":
                        self.img_steer_left.setPixmap(
                            self.mat_container["left_dark"]
                        )
                    elif self.tl_dire == "right":
                        self.img_steer_right.setPixmap(
                            self.mat_container["right_dark"]
                        )
                    self.isLightOn = True
            else:
                self.img_steer_left.setPixmap(self.mat_container["left_dark"])
                self.img_steer_right.setPixmap(self.mat_container["right_dark"])

    def convert_cv_qt(self, cv_img):
        rgb_image = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        convert_to_Qt_format = QImage(
            rgb_image.data, w, h, bytes_per_line, QImage.Format_RGB888
        )
        return QPixmap.fromImage(convert_to_Qt_format)

    def recv_data(self, data_rec):
        """
        從 DataRecievedThread 接收資料
        """
        self.cur_frame_data = data_rec
        self.flag_frame_changed = True
        # t0 = time.time()

        # 如果沒有接收到速度資料則隱藏時速表
        if "speed" not in data_rec.keys():
            data_rec["speed"] = 0
            data_rec["steering"] = 0
        else:
            self.speedometer.show()
            self.img_steer_left.show()
            self.img_steer_right.show()
            self.l_km.show()

        # 鏡頭影像解碼
        for cam, img_data in data_rec["img"].items():
            img_data = base64.b64decode(img_data)  # -> bytes
            img = np.frombuffer(img_data, np.uint8)  # -> numpy array, shape = (N,)
            img = cv2.imdecode(
                img, cv2.IMREAD_COLOR
            )  # -> numpy array, shape = (H, W, C)

            if cam == "CAM_FRONT":
                img = cv2.resize(img, (211, 119))
                self.img_front_data = self.convert_cv_qt(img)
            elif cam == "CAM_BACK":
                img = cv2.resize(img, (211, 119))
                self.img_back_data = self.convert_cv_qt(img)
            elif cam == "BEV":
                self.img_bev_data = img

        t0 = time.time()

        if self.draw_mode == "seg":
            self.cur_frame_data["dot"] = []

            img = cv2.cvtColor(self.img_bev_data, cv2.COLOR_BGR2RGB)
            img = cv2.resize(img, (200, 200))
            img = np.rot90(img, 1)

            # 去除 BEV map 中心小車圖片
            img[
                (img.shape[0] // 2 - 8) : (img.shape[0] // 2 + 8),
                (img.shape[1] // 2 - 4) : (img.shape[1] // 2 + 3),
            ] = 255

            # BEV map 二值化 (過濾雜訊顏色)
            img = cv2.threshold(img, 105, 255, cv2.THRESH_BINARY)[1]

            self.cur_frame_data["dot"] = process_bev_data(img)
            # self.cur_frame_data["dot"] = self.dummy_dot
            

        # print('==== bev img time ==== : ', round((time.time() - t0) * 1000, 4), 'ms')

    def isIntersection(self):
        """
        判斷是否進入路口
        """
        dots_data = self.cur_frame_data["dot"]

        # 定義進入路口，離開路口的點數量閾值
        in_dot_sum = 0
        out_dot_sum = 0
        THR_INTER_IN_OUT = 2

        if self.draw_mode == "seg":
            # 定義車前，後的行人穿道偵測線
            front_y_gap = [245 / 682, 275 / 682]
            back_y_gap = [395 / 682, 452 / 682]
            for dot in dots_data:
                if dot["cls"] == 1:
                    if dot["y"] > front_y_gap[0] and dot["y"] < front_y_gap[1]:
                        in_dot_sum += 1

                    if dot["y"] > back_y_gap[0] and dot["y"] < back_y_gap[1]:
                        out_dot_sum += 1

        elif self.draw_mode == "vec":
            front_y_gap = [(395 - 341) / 682, (415 - 341) / 682]  # front
            back_y_gap = [(225 - 341) / 682, (305 - 341) / 682]  # back
            for lines in dots_data:
                if lines["cls"] == 1:
                    for single_dot_x, single_dot_y in zip(lines["x"], lines["y"]):
                        # TODO : 設定x軸(橫向)範圍

                        if single_dot_y > front_y_gap[0] and single_dot_y < front_y_gap[1]:
                            in_dot_sum += 1
                        if single_dot_y > back_y_gap[0] and single_dot_y < back_y_gap[1]:
                            out_dot_sum += 1
                         
                    
        if self.flag_frame_changed:
            # 為避免鏡頭重複上升下降, 進入路口時, 若前後偵測區域都大於閾值, 只視為進入入口

            if in_dot_sum > THR_INTER_IN_OUT:
                # 加入進入路口 (in), 離開路口 (out) 狀態至 queue_inter
                if not self.queue_inter:
                    self.queue_inter.append("in")
                # 防止連續的重複狀態(in, out)
                elif self.queue_inter[-1] != "in":
                    self.queue_inter.append("in")

            if not (in_dot_sum > THR_INTER_IN_OUT and out_dot_sum > THR_INTER_IN_OUT):
                if out_dot_sum > THR_INTER_IN_OUT:
                    if not self.queue_inter:
                        self.queue_inter.append("out")
                    elif self.queue_inter[-1] != "out":
                        self.queue_inter.append("out")

            # print('queue_inter : ', self.queue_inter)

            self.flag_frame_changed = False

    def cam_rise(self):
        """
        相機視角上升
        """
        if not self.flag_cam_rised:
            if self.idx_cam_rise < 10:
                self.openGLWidget.set_view(
                    [
                        [0, 3 + self.arr_cam_ease[self.idx_cam_rise], 20],
                        [0, -1, 0],
                        [0, 1, 0],
                    ]
                )
                self.idx_cam_rise += 1
                self.flag_cam_lock = True
            else:
                self.flag_cam_rised = True
                self.flag_cam_lock = False
                self.idx_cam_rise = 0

    def cam_down(self):
        """
        相機視角下降
        """
        if self.flag_cam_rised:
            if self.idx_cam_down < 10:
                self.openGLWidget.set_view(
                    [
                        [0, 3 + 25 - self.arr_cam_ease[self.idx_cam_down], 20],
                        [0, -1, 0],
                        [0, 1, 0],
                    ]
                )
                self.idx_cam_down += 1
                self.flag_cam_lock = True
            else:
                self.flag_cam_rised = False
                self.flag_cam_lock = False
                self.idx_cam_down = 0

    def setupUi(self, MainWindow):
        super(Car_MainWindow, self).setupUi(MainWindow)

        # OpenGLWidget 設定道路線繪製模式
        self.openGLWidget.map_draw_mode = self.draw_mode

        # 前後鏡頭RGB畫面顯示
        self.img_front.setPixmap(self.convert_cv_qt(np.zeros((119, 211, 3), np.uint8)))
        self.img_back.setPixmap(self.convert_cv_qt(np.zeros((119, 211, 3), np.uint8)))

        # 時速表顯示
        self.speedometer.display(0)

        # 方向燈顯示
        self.img_steer_left.setScaledContents(True)
        self.img_steer_right.setScaledContents(True)
        self.img_speed_limit.setScaledContents(True)
        self.img_steer_left.setPixmap(self.mat_container["left_dark"])
        self.img_steer_right.setPixmap(self.mat_container["right_dark"])
        
        # 開始時隱藏時速表、方向燈
        self.speedometer.hide()
        self.img_steer_left.hide()
        self.img_steer_right.hide()
        self.l_km.hide()

        # for i in range(100):
        #         for j in range(100):
        #             self.cur_frame_data["dot"].append({"x": i / 30, "y": j / 30, "cls": 0})
        # self.dummy_dot = [{"x": i / 100, "y": j / 100, "cls": 0} for i in range(100) for j in range(100)]
        # self.dummy_dot = [(i, -5, j) for i in range(-5, 5, 1) for j in range(-5, 5, 1)]
        
    def setupTimer(self):
        self.timer_frame = QTimer()

        # 方向燈閃爍計時器
        self.timer_turnlight = QTimer()
        self.timer_turnlight.timeout.connect(self.turn_light)
        self.timer_turnlight.start(300)

        # pyqt GUI 更新計時器 (30 fps)
        self.timer_updateUI = QTimer()
        self.timer_updateUI.timeout.connect(self.updateUI)
        self.timer_updateUI.start(33)

    def load_material_img(self, mat_dict):
        mat_container = {}
        for mat_name, mat_path in mat_dict.items():
            mat_container[mat_name] = QPixmap(mat_path)
        return mat_container
    
    def updateUI(self):
        """
        更新 OpenGLWidget 畫面
        """

        # 街收到第一幀資料後才開始更新 OpenGLWidget

        if self.cur_frame_data:
            self.openGLWidget.cur_frame_data = self.cur_frame_data
            self.openGLWidget.update()

            self.update_img(self.cur_frame_data)

            # if self.draw_mode in ['seg', 'vec']:
            #     self.isIntersection()

            if self.queue_inter:
              
                if self.queue_inter[0] == "in":
                    self.cam_rise()

                    # 若鏡頭上升完成，則移除 queue_inter 中的 'in' 狀態
                    if not self.flag_cam_lock:
                        self.queue_inter.pop(0)

                elif self.queue_inter[0] == "out":
                    self.cam_down()

                    if not self.flag_cam_lock:
                        self.queue_inter.pop(0)

        # print('==== update UI time ==== : ', round((time.time() - t0) * 1000, 4), 'ms')

    def update_img(self, data_rec):
        """
        更新 pyqt GUI 畫面
        """

        self.img_front.setPixmap(self.img_front_data)
        self.img_back.setPixmap(self.img_back_data)
        self.speedometer.display(round(float(data_rec["speed"]), 1))
        

        

        if self.openGLWidget.speed_limit_60:
            self.img_speed_limit.setPixmap(self.mat_container["spd_limit_60"])


if __name__ == "__main__":
    # Dummy function to warm up the numba jit
    process_bev_data(np.zeros((200, 200, 3), np.uint8))

    sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
    app = QApplication(sys.argv)
    window = QMainWindow()
    ui = Car_MainWindow()
    ui.setupUi(window)
    window.show()
    ui.setupTimer()
    sys.exit(app.exec_())
