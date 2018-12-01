import sys
import time
import uuid
import os
import io
import math
import operator

from sonycamera import SonyCamera

from PyQt4.QtGui import *
from PyQt4.QtCore import *
from PyQt4.QtSvg import *

from PIL import Image


class LiveView(QFrame):
    INIT_WIDTH = 600.0
    INIT_HEIGHT = 400.0

    def __init__(self, parent=None):
        super(LiveView, self).__init__(parent)

        self.pixmap = QPixmap()
        self.image = None
        #self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)
        self.setLineWidth(2)
        self.setToolTip("Click image to focus camera at mouse location")
        self.setMinimumSize(LiveView.INIT_WIDTH, LiveView.INIT_HEIGHT)
        self.enabled = True
        self.previousImage = None
        self.frameCount = 0
        self.displayGrid = True

    def paintEvent(self, event):
        super(LiveView, self).paintEvent(event)

        option = QStyleOption()
        option.initFrom(self)

        # Get available screen realestate.
        h = option.rect.height()
        w = option.rect.width()

        # Create painter.
        painter = QPainter(self);
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.drawPixmap(0, 0, w, h, self.pixmap)

        if self.displayGrid:
            line1 = QLine(0, h/3, w, h/3)
            line2 = QLine(0, 2 * h/3, w, 2 * h/3)
            line3 = QLine(w/3, 0, w/3, h)
            line4 = QLine(2 * w/3, 0, 2 * w/3, h)

            painter.drawLine(line1)
            painter.drawLine(line2)
            painter.drawLine(line3)
            painter.drawLine(line4)

    def enableGrid(self, value):
        self.displayGrid = value

    def updatePixmap(self, image):
        # Update displayed image only when there is a new image available.
        if id(image) != id(self.image) and image != None:
            self.image = image
            self.pixmap.loadFromData(image)

            self.frameCount += 1

            if self.frameCount > 30:
                self.frameCount = 0

                self.detectMotion(image)

            self.update()

    def detectMotion(self, image):
        image1 = Image.open(io.BytesIO(image))
        image1.load()
        h1 = image1.histogram()

        if self.previousImage:
            h2 = self.previousImage.histogram()
            rms = math.sqrt(reduce(operator.add, map(lambda a, b: (a - b) ** 2, h1, h2)) / len(h1))

            print(int(rms / 10))

        self.previousImage = image1

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            x = (event.x() * 100.0) / self.width()
            y = (event.y() * 100.0) / self.height()
            self.emit(SIGNAL('clicked(int, int)'), x, y)



class MyMainWindow(QWidget):
    def __init__(self, parent):
        QWidget.__init__(self)

        # Timer used to update progress bar when foto is downloaded from camera.
        self.timer = QTimer()
        self.connect(self.timer, SIGNAL("timeout()"), self.updateProgressBar)

        # Create camera handler and run it in a separate thread.
        self.cameraThread = QThread()
        self.camera = SonyCamera()
        self.camera.moveToThread(self.cameraThread)
        self.cameraThread.start()

        # Setup various GUI components, connect signals, etc.
        self.createGuiWidgets()
        self.changeGuiState(False)

        # Connect things up
        self.camera.newPreviewImageSignal.connect(self.liveView.updatePixmap)
        self.camera.liveViewRunningSignal.connect(self.connectedToCamera)
        self.camera.newFotoSignal.connect(self.handleNewFoto)
        self.camera.liveViewStoppedSignal.connect(self.stopLiveView)

        # Now start camera connection in camera thread which will also kickoff the liveview.
        self.camera.startCamera()

    def createGuiWidgets(self):
        self.liveView = LiveView(self)
        self.connect(self.liveView, SIGNAL('clicked(int, int)'), self.setFocus)

        x = self.liveView.width() / 2.0 - 200
        y = self.liveView.height() / 2.0
        self.imageUploadProgressBar = QProgressBar(self.liveView)
        self.imageUploadProgressBar.setOrientation(Qt.Horizontal)
        self.imageUploadProgressBar.setMinimum(0)
        self.imageUploadProgressBar.setMaximum(100)
        self.imageUploadProgressBar.setValue(0)
        self.imageUploadProgressBar.move(x, y)
        self.imageUploadProgressBar.setFixedSize(400, 20)
        self.imageUploadProgressBar.hide()

        # Still size combo box.
        self.stillSizeCombo = QComboBox()
        self.stillSizeCombo.currentIndexChanged['QString'].connect(self.changeStillSize)
        self.stillSizeCombo.setToolTip("Select still photo size")

        # Shoot mode combo box.
        self.shootModeCombo = QComboBox()
        self.shootModeCombo.currentIndexChanged['QString'].connect(self.changeShootMode)
        self.shootModeCombo.setToolTip("Select shoot mode")

        # --------------------------------Start Movie Rec button----------------------------
        self.startRecButton = QPushButton("Start Rec", self)
        self.startRecButton.setToolTip("Press to start video recording.")
        self.connect(self.startRecButton, SIGNAL("clicked()"), self.startVideo)

        # --------------------------------Stop Movie Rec button-----------------------------
        self.stopRecButton = QPushButton("Stop Rec", self)
        self.stopRecButton.setToolTip("Press to stop video recording.")
        self.connect(self.stopRecButton, SIGNAL("clicked()"), self.stopVideo)

        # --------------------------------Snap photo button---------------------------------
        self.snapButton = QPushButton("Take Photo", self)
        self.snapButton.setToolTip("Press to take a photo. Image will automagically be uploaded to computer.")
        self.connect(self.snapButton, SIGNAL("clicked()"), self.takePhoto)

        # --------------------------------Zoom buttons---------------------------------
        self.zoomOutButton = QPushButton("Zoom Out")
        self.zoomOutButton.setToolTip("Press and hold for continuous zoom out")
        self.connect(self.zoomOutButton, SIGNAL("pressed()"), self.zoomOutStart)
        self.connect(self.zoomOutButton, SIGNAL("released()"), self.zoomOutStop)

        self.zoomInButton = QPushButton("Zoom In")
        self.zoomInButton.setToolTip("Press and hold for continuous zoom in")
        self.connect(self.zoomInButton, SIGNAL("pressed()"), self.zoomInStart)
        self.connect(self.zoomInButton, SIGNAL("released()"), self.zoomInStop)

        # --------------------------------Show grid button---------------------------------
        self.gridButton = QPushButton("Show Grid", self)
        self.gridButton.setCheckable(True)
        self.gridButton.setChecked(True)
        self.gridButton.setToolTip("Press to display rule of 1/3 grid.")
        self.connect(self.gridButton, SIGNAL("clicked()"), self.enableGrid)

        # --------------------------------Connect to camera button---------------------------------
        self.connectButton = QPushButton("Connect to Camera", self)
        self.connectButton.setToolTip("Press to connect to camera.")
        self.connect(self.connectButton, SIGNAL("clicked()"), self.camera.startCamera)

        # ---------------------------------Warning message if camera is not connected-----------------
        self.connectMessage = QLabel("Camera not found. Check that camera is connected via WIFI and then click the connect button below.")
        self.connectMessage.setWordWrap(True)

        # --------------------------------Layout everything-----------------------------
        mainlayout = QGridLayout(self)
        self.setLayout(mainlayout)

        vlayout = QVBoxLayout()
        vlayout.addWidget(self.shootModeCombo)
        vlayout.addWidget(self.startRecButton)
        vlayout.addWidget(self.stopRecButton)
        vlayout.addWidget(self.snapButton)
        vlayout.addWidget(self.zoomInButton)
        vlayout.addWidget(self.zoomOutButton)
        vlayout.addWidget(self.gridButton)
        vlayout.addWidget(self.stillSizeCombo)
        vlayout.addStretch(1)
        vlayout.addWidget(self.connectMessage)
        vlayout.addWidget(self.connectButton)

        mainlayout.addWidget(self.liveView, 0, 0)
        mainlayout.setColumnStretch(0,0)
        mainlayout.setColumnStretch(0,1)
        mainlayout.addLayout(vlayout, 0, 1)

    def changeGuiState(self, state):
        self.shootModeCombo.setEnabled(state)
        self.startRecButton.setEnabled(False)
        self.stopRecButton.setEnabled(False)
        self.snapButton.setEnabled(state)
        self.zoomOutButton.setEnabled(state)
        self.zoomInButton.setEnabled(state)
        self.liveView.setEnabled(state)
        self.connectMessage.setVisible(not state)
        self.connectButton.setEnabled(not state)
        self.gridButton.setEnabled(state)
        self.stillSizeCombo.setEnabled(state)

        if not state:
            self.imageUploadProgressBar.hide()
            self.stillSizeCombo.clear()
            self.shootModeCombo.clear()

    def enableGrid(self):
        if self.gridButton.isChecked():
            self.liveView.enableGrid(True)
        else:
            self.liveView.enableGrid(False)

    def zoomInStart(self):
        self.camera.sendCameraCommand('actZoom', ['in', 'start'])

    def zoomInStop(self):
        self.camera.sendCameraCommand('actZoom', ['in', 'stop'])

    def zoomOutStart(self):
        self.camera.sendCameraCommand('actZoom', ['out', 'start'])

    def zoomOutStop(self):
        self.camera.sendCameraCommand('actZoom', ['out', 'stop'])

    def setFocus(self, x, y):
        v = self.camera.sendCameraCommand('setTouchAFPosition', [x, y])

    def changeStillSize(self):
        index = self.stillSizeCombo.currentIndex()
        aspect = self.camera.supportedStillSizes[index]['aspect']
        size = self.camera.supportedStillSizes[index]['size']
        print(aspect,size)
        self.camera.sendCameraCommand('setStillSize', ['%s' % aspect, '%s' % size])

    def changeShootMode(self):
        index = self.shootModeCombo.currentIndex()
        if index == 0:
            self.camera.stillMode()
            self.startRecButton.setEnabled(False)
            self.stopRecButton.setEnabled(False)
        elif index == 1:
            self.camera.videoMode()
            self.startRecButton.setEnabled(True)
            self.stopRecButton.setEnabled(True)

    def connectedToCamera(self):
        self.changeGuiState(True)

        self.shootModeCombo.clear()
        modeList = ['Still', 'Video']
        for modeItem in modeList:
            self.shootModeCombo.addItem(modeItem)

        # Clear combobox first.
        self.stillSizeCombo.clear()

        if self.camera.supportedStillSizes:
            self.comboList = ['Aspect Ratio: %s, Size: %s' % (d['aspect'], d['size']) for d in self.camera.supportedStillSizes]
            self.stillSizeCombo.addItems(self.comboList)

    def stopLiveView(self):
        self.changeGuiState(False)

    def startVideo(self):
        self.startRecButton.setEnabled(False)
        self.camera.startVideo()

    def stopVideo(self):
        self.startRecButton.setEnabled(True)
        self.camera.stopVideo()

    def takePhoto(self):
        self.snapButton.setEnabled(False)
        image = self.camera.takePhoto()
        self.timer.start(100)

    def updateProgressBar(self):
        x = self.liveView.width() / 2.0 - 200
        y = self.liveView.height() / 2.0
        self.imageUploadProgressBar.move(x, y)
        self.imageUploadProgressBar.setValue(self.camera.photoUploadPercent)
        self.imageUploadProgressBar.show()

    def handleNewFoto(self, imageData):
        self.timer.stop()
        self.imageUploadProgressBar.hide()
        self.snapButton.setEnabled(True)

        if imageData:
            u = uuid.uuid1().fields[0]
            filename = '{0}.jpg'.format(u)
            path = os.path.join('.', 'DCIM')

            try:
                os.makedirs(path)

            except OSError:
                # Path already exists.
                if not os.path.isdir(path):
                    # DCIM is the name of a file.
                    print("File with name DCIM already exists.")
                    path = '.'

            newFilePath = os.path.join(path, filename)

            try:
                with open(newFilePath, 'wb') as f:
                    f.write(imageData)
            except:
                print("Unable to save file %s.jpg" % newFilePath)


#---------------------------------------------------Main--------------------------------------------
def main(args):
    app=QApplication(args)
    win = MyMainWindow(app)
    app.connect(app, SIGNAL("lastWindowClosed()"), app, SLOT("quit()"))
    win.show()
    app.exec_()



#--------------------------------------------------End of Main---------------------------------------------


#Run this as a script if running stand alone
if __name__=="__main__":
    main(sys.argv)

