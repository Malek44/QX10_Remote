import sys
import socket
import urllib.parse
import json
import queue
import time

from queue import Queue

from lxml import etree
from PyQt4.QtGui import *
from PyQt4.QtCore import *
from PyQt4.QtSvg import *


def cmp_to_key(mycmp):
    'Convert a cmp= function into a key= function'
    class K(object):
        def __init__(self, obj, *args):
            self.obj = obj
        def __lt__(self, other):
            return mycmp(self.obj, other.obj) < 0
        def __gt__(self, other):
            return mycmp(self.obj, other.obj) > 0
        def __eq__(self, other):
            return mycmp(self.obj, other.obj) == 0
        def __le__(self, other):
            return mycmp(self.obj, other.obj) <= 0  
        def __ge__(self, other):
            return mycmp(self.obj, other.obj) >= 0
        def __ne__(self, other):
            return mycmp(self.obj, other.obj) != 0
    return K


class SonyCamera(QObject):
    newPreviewImageSignal = pyqtSignal(object)
    newFotoSignal = pyqtSignal(object)
    liveViewRunningSignal = pyqtSignal(object)
    liveViewStoppedSignal = pyqtSignal(object)

    SERVICE                            = "urn:schemas-sony-com:service:ScalarWebAPI:1"
    SSDP_IP                            = '239.255.255.250'
    SSDP_PORT                          = 1900
    NUM_LIVEVIEW_HEADER_BYTES          = 8
    NUM_LIVEVIEW_PAYLOAD_HEADER_BYTES  = 128
    PAYLOAD_SIZE_INDEX                 = 4
    CHUNK_SIZE                         = 4096

    def __init__(self):
        self.getNextLiveViewImageEvent = QEvent.registerEventType()
        self.initCameraConnectionEvent = QEvent.registerEventType()
        self.cameraCommandEvent = QEvent.registerEventType()
        self.takeFotoEvent = QEvent.registerEventType()
        self.setStillShootModeEvent = QEvent.registerEventType()
        self.setVideoShootModeEvent = QEvent.registerEventType()
        self.startMovieRecEvent = QEvent.registerEventType()
        self.stopMovieRecEvent = QEvent.registerEventType()

        # Camera command queue.
        self.commandQueue = Queue()

        super(SonyCamera, self).__init__()

    def event(self, event):
        """Main event handler for this QObject.  Events are used to kickoff background processing inside thread."""
        t = event.type()

        if event.type() not in (self.getNextLiveViewImageEvent,
                                self.initCameraConnectionEvent,
                                self.cameraCommandEvent,
                                self.setStillShootModeEvent,
                                self.setVideoShootModeEvent,
                                self.startMovieRecEvent,
                                self.stopMovieRecEvent,
                                self.takeFotoEvent):
            return super(SonyCamera, self).event(event)

        event.accept()

        if t == self.getNextLiveViewImageEvent:
            self._liveViewEventHandler()
        elif t == self.initCameraConnectionEvent:
            self._connectToCamera()
        elif t == self.cameraCommandEvent:
            self._handleCameraCommandEvent()
        elif t == self.takeFotoEvent:
            self._handleTakeFotoEvent()
        elif t == self.setStillShootModeEvent:
            self._handleSetShootModeEvent('still')
        elif t == self.setVideoShootModeEvent:
            self._handleSetShootModeEvent('movie')
        elif t == self.startMovieRecEvent:
            self._handleStartMovieRecEvent()
        elif t == self.stopMovieRecEvent:
            self._handleStopMovieRecEvent()
        else:
            pass

        return True

    def startCamera(self):
        print("starting camera")
        QApplication.postEvent(self, QEvent(self.initCameraConnectionEvent), Qt.LowEventPriority - 1)

    def _connectToCamera(self):
        self.SSDPInfo = {}
        self.liveViewActive = False
        self.photoUploadPercent = 0
        self.cameraUrl = None
        self.supportedStillSizes = None

        # Use Simple Service Discovery Protocol (SSDP) to find camera, ping it to get info and URLs for communicating with it.
        if self._getCameraInfo(SonyCamera.SERVICE):
            self.availableApiList = self._sendCameraCommand("getAvailableApiList", [])
            self._getSupportedStillSizes()

            # Tell camera to start live view. Call this before starting threads.
            self._startLiveView()

            if self.liveViewActive:
                QApplication.postEvent(self, QEvent(self.getNextLiveViewImageEvent), Qt.LowEventPriority - 1)

    def _getCameraInfo(self, service, timeout=1, retries=3):
        retVal = False

        # Simple service discovery protocol message.
        messageTemplate = "\r\n".join([
            'M-SEARCH * HTTP/1.1',
            'HOST: {0}:{1}',
            'MAN: "ssdp:discover"',
            'MX: 1',
            'ST: {st}',
            '',
            ''])

        # Create message string.
        message = messageTemplate.format(SonyCamera.SSDP_IP, SonyCamera.SSDP_PORT, st=service)

        for retry in range(retries):
            print(("Retry: %d" % retry))

            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

            except socket.error:
                sock = None

            else:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                    sock.settimeout(timeout)
                    sock.sendto(bytes(message, 'UTF-8'), (SonyCamera.SSDP_IP, SonyCamera.SSDP_PORT))

                except socket.error:
                    sock.close()
                    sock = None

            if sock:
                try:
                    responseString = sock.recv(1024).decode('utf8')

                except socket.error:
                    responseString = ''

                finally:
                    sock.close()

                # Check we got useful info from SSDP response, including URL of camera XML document.
                if self._getSSDPResponse(responseString) and self._getCameraXmlDoc():
                    retVal = True
                    break

        return retVal

    def _getSSDPResponse(self, message):
        discoveryData = message.splitlines()
        retVal = False

        for line in discoveryData:
            lowerline = line.lower()

            if lowerline.startswith('location: '):
                self.SSDPInfo['location'] = line.split(': ', 1)[1]
                retVal = True

            elif lowerline.startswith('server: '):
                self.SSDPInfo['server'] = line.split(': ', 1)[1]

            elif lowerline.startswith('st: '):
                self.SSDPInfo['st'] = line.split(': ', 1)[1]

            elif lowerline.startswith('usn: '):
                self.SSDPInfo['usn'] = line.split(': ', 1)[1]

            elif lowerline.startswith('cache-control: '):
                self.SSDPInfo['cache-control'] = line.split(': ', 1)[1]

        return retVal

    def _getCameraXmlDoc(self):
        retVal = False

        # Use contents of SSDP response to get camera XML document.
        url = urllib.parse.urlparse(self.SSDPInfo['location'])

        # Get IP address and port number of camera XML document.
        temp = url.netloc.split(':')
        HOST = temp[0]
        PORT = int(temp[1])

        commandString = "GET %s HTTP/1.0\r\nHost: %s\r\n\r\n" % (url.path, HOST)

        sock = self._createSockAndSend((socket.AF_INET, socket.SOCK_STREAM), HOST, PORT, bytes(commandString, 'UTF-8'))

        if sock:
            try:
                httpHeader = sock.recv(SonyCamera.CHUNK_SIZE)

            except socket.error:
                httpHeader = ''

            numBytes = self._getMessageLengthField(httpHeader)

            cameraXmlDataString = self._recvAllData(sock, numBytes)
            sock.close()

            if cameraXmlDataString:
                # Parse XML string returned by camera.
                self.cameraXmlDoc = etree.fromstring(cameraXmlDataString)

                # Parse cameraXmlDoc to get URL for camera API commands.
                retVal = self._getCameraUrls()

        return retVal

    def _getCameraUrls(self):
        retVal = False

        # Parse camera XML document.
        serviceUrls = {}

        # Scan XML document from camera for relevant URLs, e.g. the URL to send Song API commands to.
        for i, e in enumerate(self.cameraXmlDoc.iter("{urn:schemas-sony-com:av}X_ScalarWebAPI_Service")):
            temp = list(e.iter("{urn:schemas-sony-com:av}X_ScalarWebAPI_ServiceType", "{urn:schemas-sony-com:av}X_ScalarWebAPI_ActionList_URL"))

            if len(temp) >= 2:
                serviceUrls[temp[0].text] = temp[1].text

        if 'camera' in serviceUrls:
            # Extract camera URL. This is where the camera API commands are sent to.
            pathString = '/'.join([serviceUrls['camera'], 'camera'])
            self.cameraUrl = urllib.parse.urlparse(pathString)

            # Get camera command API URL and port number.
            temp = self.cameraUrl.netloc.split(':')

            if len(temp) == 2:
                self.cameraCommandHost = temp[0]
                self.cameraCommandPort = int(temp[1])
                retVal = True

        return retVal

    def _getMessageLengthField(self, headerString):
        payloadLength = 0

        headerLines = headerString.splitlines()

        for line in headerLines:
            lowerline = line.lower()

            if lowerline.startswith(bytes('content-length: ', 'UTF-8')):
                payloadLength = int(line.split(bytes(': ', 'UTF-8'), 1)[1])

        return payloadLength

    def _getSupportedStillSizes(self):
        sizes = self._sendCameraCommand("getSupportedStillSize", [])[0]

        print(sizes)

        def mcmp(d1, d2):
            x = int(d1['size'].rstrip('M'))
            y = int(d2['size'].rstrip('M'))

            if x < y:
                return 1

            elif x > y:

                return -1

            else:
                return 0

        if sizes:
            sizes.sort(key=cmp_to_key(mcmp))
            self.supportedStillSizes = sizes

        else:
            self.supportedStillSizes = None

    def _startLiveView(self):
        self.liveViewActive = False

        responseJsonValue = self._sendCameraCommand("startLiveview", [])

        if responseJsonValue:
            self.liveViewUrl = responseJsonValue[0]

            # Parse URL, extract info.
            url = urllib.parse.urlparse(self.liveViewUrl)

            # Get IP address and port number of live view server on camera.
            temp = url.netloc.split(':')

            if len(temp) == 2:
                HOST = temp[0]
                PORT = int(temp[1])

                imagePath = ''.join([url.path, '?', url.query])
                commandString = "GET %s HTTP/1.0\r\nHost: %s\r\n\r\n" % (imagePath, HOST)

                sock = self._createSockAndSend((socket.AF_INET, socket.SOCK_STREAM), HOST, PORT, bytes(commandString, 'UTF-8'))

                if sock:
                    # Keep live view socket open.
                    self.liveViewSock = sock

                    try:
                        # Receive live view header.
                        httpHeader = sock.recv(SonyCamera.CHUNK_SIZE)
                        self.liveViewActive = True
                        self.liveViewRunningSignal.emit(True)

                    except socket.error:
                        sock.close()

        # Signal that liveview has quit.
        if not self.liveViewActive:
            self.liveViewStoppedSignal.emit(True)

    def _liveViewEventHandler(self):
        if self.liveViewActive:
            try:
                commonHeader = self.liveViewSock.recv(SonyCamera.NUM_LIVEVIEW_HEADER_BYTES)
                payloadHeader = self.liveViewSock.recv(SonyCamera.NUM_LIVEVIEW_PAYLOAD_HEADER_BYTES)

                # Returns 0 if headers are corrupted.
                totalNumBytesToGet = self._parseLiveViewHeaders(commonHeader, payloadHeader)

            except socket.error as msg:
                totalNumBytesToGet = 0

            # Returns empty string if upload failed. Returns empty string if totalNumBytesToGet == 0.
            image = self._recvAllData(self.liveViewSock, totalNumBytesToGet)

            if image:
                self.newPreviewImageSignal.emit(image)

            else:
                # Restart live view if any error occurs.
                self._startLiveView()

            if self.liveViewActive:
                # Post event to trigger next preview image capture.
                QApplication.postEvent(self, QEvent(self.getNextLiveViewImageEvent), Qt.LowEventPriority - 1)

    def _parseLiveViewHeaders(self, commonHeader, payloadHeader):
        # Check live view frame headers are sensible.
        if len(commonHeader) == SonyCamera.NUM_LIVEVIEW_HEADER_BYTES and \
           commonHeader[0] == 0xFF and \
           commonHeader[1] == 0x01 and \
           len(payloadHeader) == SonyCamera.NUM_LIVEVIEW_PAYLOAD_HEADER_BYTES and \
           payloadHeader[0] == 0x24 and \
           payloadHeader[1] == 0x35 and \
           payloadHeader[2] == 0x68 and \
           payloadHeader[3] == 0x79:
            # Compute payload length from 3 byte field..
            totalNumBytesToGet = payloadHeader[SonyCamera.PAYLOAD_SIZE_INDEX] * 256 * 256 + \
                                 payloadHeader[SonyCamera.PAYLOAD_SIZE_INDEX+1] * 256 + \
                                 payloadHeader[SonyCamera.PAYLOAD_SIZE_INDEX+2]

        else:
            totalNumBytesToGet = 0
            print("Header parse error")

        return totalNumBytesToGet

    def sendCameraCommand(self, methodStr, paramsList):
        # Put command on queue.
        self.commandQueue.put((methodStr, paramsList))

        """Call this method from outside world to send a command to camera inside thread."""
        QApplication.postEvent(self, QEvent(self.cameraCommandEvent), Qt.LowEventPriority - 1)

    def _handleCameraCommandEvent(self):
        while not self.commandQueue.empty():
            self._sendCameraCommand(*self.commandQueue.get())

    def _sendCameraCommand(self, methodStr, paramsList):
        retVal = None
        jsonData = {
                       "method": methodStr,
                       "params": paramsList,
                       "id": 1,
                       "version": "1.0"
                   }

        jsonDataString = json.dumps(jsonData)
        commandString = "POST %s HTTP/1.1\r\nHost: %s\r\nContent-Length: %d\r\n\r\n" % (self.cameraUrl.path, self.cameraCommandHost, len(jsonDataString))

        # Setup socket.
        sock = self._createSockAndSend((socket.AF_INET, socket.SOCK_STREAM), self.cameraCommandHost, self.cameraCommandPort, bytes(commandString + jsonDataString, 'UTF-8'))

        # Socket created and command successfully sent?
        if sock:
            # Get response from camera (includes header).
            try:
                commandResponseString = sock.recv(SonyCamera.CHUNK_SIZE)

            except socket.error:
                print("sock error")
                sock.close()

            else:
                # Extract message header and json data.
                header, _, jsonResponseString = commandResponseString.partition(bytes('\r\n\r\n', 'UTF-8'))

                # Get header and payload lengths
                payloadLength = self._getMessageLengthField(header)

                # How many bytes left to get?  Include 4 bytes for \r\n\r\n chars between header and message data.
                remainingBytesToGet = len(header) + 4 + payloadLength - len(commandResponseString)

                # Get more bytes of the command response?
                if remainingBytesToGet:
                    commandResponseString += self._recvAllData(sock, remainingBytesToGet)

                    # Extract packet header and JSON data.
                    header, _, jsonResponseString = commandResponseString.partition(bytes('\r\n\r\n', 'UTF-8'))

                # Parse JSON string to create JSON object.
                jsonCommandResponse = json.loads(jsonResponseString.decode('utf8'))

                if 'error' in jsonCommandResponse:
                    errorCode = jsonCommandResponse['error'][0]
                    errorMessage = jsonCommandResponse['error'][1]
                    print("sendCommand: Got error response")
                    print(("sendCommand: Method = %s" % methodStr))
                    print(("sendCommand: Params = %s" % paramsList))
                    print(("sendCommand: Error code = %d" % errorCode))
                    print(("sendCommand: Error message = %s" % errorMessage))
                    retVal = None

                elif 'result' in jsonCommandResponse:
                    retVal = jsonCommandResponse['result']

                elif 'results' in jsonCommandResponse:
                    retVal = jsonCommandResponse['results']

                else:
                    retVal = jsonCommandResponse

                sock.close()

        return retVal

    def _createSockAndSend(self, socketType, HOST, PORT, data):
        try:
            sock = socket.socket(*socketType)

        except socket.error:
            return None

        try:
            sock.settimeout(8.0)
            sock.connect((HOST, PORT))

        except socket.error:
            sock.close()
            return None

        try:
            sock.send(data)

        except socket.error:
            sock.close()
            return None

        return sock

    def _recvAllData(self, sock, totalNumBytesToGet):
        payload = b''

        while totalNumBytesToGet:
            if totalNumBytesToGet > SonyCamera.CHUNK_SIZE:
                numBytesToGet = SonyCamera.CHUNK_SIZE
            else:
                numBytesToGet = totalNumBytesToGet

            # Read data on socket.
            try:
                data = sock.recv(numBytesToGet)
            except socket.error as msg:
                data = b''

            # Try succeeded.
            if len(data) == 0:
                payload = b''
                break

            else:
                # Append data to end of payload.
                payload = payload + data

                # Subtract actual number of bytes read.
                totalNumBytesToGet -= len(data)

        return payload

    def stillMode(self):
        """Call this method from outside world to send a start video command to camera inside thread."""
        QApplication.postEvent(self, QEvent(self.setStillShootModeEvent), Qt.LowEventPriority - 1)

    def videoMode(self):
        """Call this method from outside world to send a start video command to camera inside thread."""
        QApplication.postEvent(self, QEvent(self.setVideoShootModeEvent), Qt.LowEventPriority - 1)

    def takePhoto(self):
        """Call this method from outside world to send a take foto command to camera inside thread."""
        QApplication.postEvent(self, QEvent(self.takeFotoEvent), Qt.LowEventPriority - 1)

    def startVideo(self):
        """Call this method from outside world to send a start video command to camera inside thread."""
        QApplication.postEvent(self, QEvent(self.startMovieRecEvent), Qt.LowEventPriority - 1)

    def stopVideo(self):
        """Call this method from outside world to send a start video command to camera inside thread."""
        QApplication.postEvent(self, QEvent(self.stopMovieRecEvent), Qt.LowEventPriority - 1)

    def _handleSetShootModeEvent(self, mode):
        cameraStatus = self._sendCameraCommand("getEvent", [False])

        if cameraStatus[1]['cameraStatus'] == 'IDLE':
            ret = self._sendCameraCommand("setShootMode", [mode])

            if ret[0] != 0:
                print("ERROR: Unsuccessful change of shoot mode to %s" % mode)

            print(ret)

        else:
            print("ERROR: Operation aborted, camera not in IDLE state, current state: %s" % cameraStatus[1]['cameraStatus'])

    def _handleStartMovieRecEvent(self):
        cameraStatus = self._sendCameraCommand("getEvent", [False])

        if cameraStatus[1]['cameraStatus'] == 'IDLE':

            if cameraStatus[21]['currentShootMode'] == 'movie':
                ret = self._sendCameraCommand("startMovieRec", [])

                if ret[0] != 0:
                    print("ERROR: Cannot start Movie recording")

                print(ret)

            else:
                print("ERROR: Shooting mode must be set to Movie before start recording")

        else:
            print("ERROR: Operation [StartMovieRec] aborted, camera not in IDLE state, current state: %s" % cameraStatus[1]['cameraStatus'])

    def _handleStopMovieRecEvent(self):
        cameraStatus = self._sendCameraCommand("getEvent", [False])

        if cameraStatus[1]['cameraStatus'] == 'MovieRecording':
            snapVideo = self._sendCameraCommand("stopMovieRec", [])

        else:
            print("ERROR: Operation [StopMovieRec] aborted, camera not in MovieRecording state, current state: %s" % cameraStatus[1]['cameraStatus'])

    def _handleTakeFotoEvent(self):
        self.photoUploadPercent = 0

        cameraStatus = self._sendCameraCommand("getEvent", [False])

        if cameraStatus[1]['cameraStatus'] == 'IDLE':
            snapShot = self._sendCameraCommand("actTakePicture", [])
            print(snapShot)
            self.photoUploadPercent = 10
            waitForCamera = True

            # Wait for camera to complete taking photo.
            while waitForCamera:
                cameraStatus = self._sendCameraCommand("getEvent", [False])

                if cameraStatus[1]['cameraStatus'] == 'IDLE':
                    # Camera has completed taking picture.
                    waitForCamera = False

                    # Parse URL, extract info.
                    url = urllib.parse.urlparse(snapShot[0][0])

                    # Get IP address and port number of live view server on camera.
                    temp = url.netloc.split(':')

                    if len(temp) == 2:
                        HOST = temp[0]
                        PORT = int(temp[1])

                        imagePath = ''.join([url.path, '?', url.query])

                        commandString = "GET %s HTTP/1.0\r\nHost: %s\r\n\r\n" % (imagePath, HOST)

                        sock = self._createSockAndSend((socket.AF_INET, socket.SOCK_STREAM), HOST, PORT, bytes(commandString, 'UTF-8'))

                        if sock:
                            httpHeader = sock.recv(SonyCamera.CHUNK_SIZE)

                            self.photoUploadPercent = 20
                            payloadLength = self._getMessageLengthField(httpHeader)
                            image = b''
                            totalNumBytesToGet = payloadLength

                            while totalNumBytesToGet:
                                if totalNumBytesToGet > SonyCamera.CHUNK_SIZE:
                                    numBytesToGet = SonyCamera.CHUNK_SIZE
                                else:
                                    numBytesToGet = totalNumBytesToGet

                                # Read data on socket.
                                try:
                                    data = sock.recv(numBytesToGet)

                                except socket.error as msg:
                                    data = b''

                                # Check we got some data.
                                if len(data) == 0:
                                    image = b''
                                    break

                                else:
                                    # Append data to end of image.
                                    image = image + data

                                    # Subtract actual number of bytes read.
                                    totalNumBytesToGet -= len(data)

                                    percentageUploaded = int(((payloadLength - totalNumBytesToGet) * 100.0) / payloadLength)

                                    self.photoUploadPercent = 20 + percentageUploaded * 0.8

                            # Save photo if all data received.
                            if len(image) == payloadLength:
                                self.newFotoSignal.emit(image)

                            sock.close()


