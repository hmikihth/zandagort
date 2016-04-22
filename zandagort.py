#!/usr/bin/env python3
#-*- coding:utf-8 -*-

"""
Zandagort server

Usage:
python server.py

GET command:
<command>?<arguments>
<arguments>= <key>=<value>[&<key>=<value>]*

POST command:
<command> in header, <arguments> in body
<arguments>= JSON

Response on error:
{"error": "<error message>"}

Response on success:
{"response": <response_object>}

Architecture:
client (browser or command line)
|http|
zanda server:
- request threads: get input from client, queue to main thread, send back response to client
- server thread: create request threads
|Queue|
- main thread: read/write data (Game) through controllers
|Queue|
- cron thread: initiate internal commands like sim() and dump()

Request-response flow:
client
|http request|
ZandagortHTTPServer
|threading|
ZandagortRequestHandler.do_GET
    ZandagortRequestHandler._get_response
        |Queue|
        ZandagortServer.server_forever
            ZandagortServer._execute_client_request
                <Get/Post>Controller.<some_method>
    ZandagortRequestHandler._send_response
|http response|
client
"""

import errno
import sys
import traceback
import json
import datetime
import http.cookies
from urllib.parse import urlparse, parse_qs
import queue
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from socket import error as socket_error

import config
from game import Game
from mycron import MyCron
from myenum import MyEnum
from getcontroller import GetController
from postcontroller import PostController
from utils import create_request_string


InnerCommands = MyEnum("InnerCommands", names=["Sim", "Dump"])

ErrorCodes = MyEnum("ErrorCodes", names=["ArgumentSyntaxError"])


def _parse_qs_flat(query):
    """Return flat version of parse_qs. 'q=a,b' becomes "q":"a,b" not "q":["a","b"]"""
    deep_query_dict = parse_qs(query)
    flat_query_dict = {}
    for key, deep_value in deep_query_dict.items():
        flat_query_dict[key] = deep_value[0]
    return flat_query_dict


class ZandagortRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for ZandagortHTTPServer"""
    
    server_version = config.SERVER_VERSION
    
    def do_GET(self):
        """Handle GET requests"""
        url = urlparse(self.path)
        command = url.path.lstrip("/")
        if command == "test" or command == "test/":
            self._send_static_file("html/test.html")
            return
        if command.startswith("static/"):
            self._send_static_file(command[7:])
            return
        if command == "favicon.ico":
            self._send_static_file("img/favicon.ico", "image/x-icon")
            return
        try:
            arguments = _parse_qs_flat(url.query)
        except Exception:
            arguments = ErrorCodes.ArgumentSyntaxError
        auth_cookie_value = self._get_auth_cookie_value()
        response = self._get_response("GET", command, arguments, auth_cookie_value)
        self._send_response(response)
    
    def do_POST(self):
        """Handle POST requests"""
        command = self.path.lstrip("/")
        try:
            request_body_length = int(self.headers.getheader("content-length"))
        except TypeError:
            request_body_length = 0
        try:
            arguments = json.loads(self.rfile.read(request_body_length))
        except Exception:
            arguments = ErrorCodes.ArgumentSyntaxError
        auth_cookie_value = self._get_auth_cookie_value()
        response = self._get_response("POST", command, arguments, auth_cookie_value)
        self._send_response(response)
    
    def log_message(self, format_, *args):
        """Overwrite (disable) default logging"""
        pass
    
    def _get_auth_cookie_value(self):
        """Get auth cookie value from HTTP headers"""
        auth_cookie_value = ""
        if "Cookie" in self.headers:
            cookies = http.cookies.SimpleCookie(self.headers["Cookie"])
            if config.AUTH_COOKIE_NAME in cookies:
                auth_cookie_value = cookies[config.AUTH_COOKIE_NAME].value
        return auth_cookie_value
    
    def _get_response(self, method, command, arguments, auth_cookie_value):
        """Get response from core Zandagort Server"""
        my_queue = queue.Queue()
        self.server.request_queue.put({
            "response_queue": my_queue,
            "method": method,
            "command": command,
            "arguments": arguments,
            "auth_cookie_value": auth_cookie_value,
            "client_ip": self.client_address[0],
        })
        response = my_queue.get()
        my_queue.task_done()
        return response
    
    def _send_response(self, response, content_type="application/json", raw=False):
        """Send response to client"""
        auth_cookie_value = None
        if raw:
            response_text = response
        else:
            try:
                if "auth_cookie_value" in response:
                    auth_cookie_value = response["auth_cookie_value"]
                    del response["auth_cookie_value"]
            except TypeError:
                pass
            response_text = json.dumps(response)
        self.send_response(200)
        self.send_header("Content-type", content_type + "; charset=utf-8")
        self.send_header("Content-length", str(len(response_text)))
        # TODO: "Cache-Control: no-cache" "Expires: -1" ???
        if auth_cookie_value is not None:
            if auth_cookie_value == "":
                self._send_cookie(config.AUTH_COOKIE_NAME, auth_cookie_value, -3600, "/")  # delete cookie if explicitly indicated by server
            else:
                self._send_cookie(config.AUTH_COOKIE_NAME, auth_cookie_value, config.AUTH_COOKIE_EXPIRY, "/")
        self.end_headers()
        self.wfile.write(response_text)
    
    def _send_cookie(self, cookie_key, cookie_value, expires_from_now, path):
        """Send cookie with key, value, expiry and path"""
        cookie = http.cookies.SimpleCookie()
        cookie[cookie_key] = cookie_value
        expires = datetime.datetime.now() + datetime.timedelta(seconds=expires_from_now)
        cookie[cookie_key]["expires"] = expires.strftime("%a, %d-%b-%Y %H:%M:%S GMT")
        cookie[cookie_key]["path"] = path
        self.send_header("Set-Cookie", cookie.output(header=""))
    
    def _send_static_file(self, filename, content_type=None):
        """Send static file to client"""
        content = ""
        if content_type is None:
            try:
                file_type = filename[:filename.index("/")]
            except ValueError:
                file_type = "?"
            if file_type == "js":
                content_type = "text/javascript"
            elif file_type == "css":
                content_type = "text/css"
            else:
                content_type = "text/html"
        with open("static/" + filename, "rb") as infile:
            content = infile.read()
        self._send_response(content, content_type, True)


class ZandagortHTTPServer(ThreadingMixIn, HTTPServer):
    """Multi threaded HTTP server between clients and ZandagortServer"""
    
    def __init__(self, server_address, request_queue):
        HTTPServer.__init__(self, server_address, ZandagortRequestHandler)  # can't use standard super() because HTTPServer is old-style class
        self.request_queue = request_queue
        self.daemon_threads = True


class ZandagortServer(object):
    """Single threaded core server to handle Game"""
    
    def __init__(self, host, port):
        self._address = (host, port)
        self._request_queue = queue.Queue()
        self._server = ZandagortHTTPServer(self._address, self._request_queue)
        self._server_thread = threading.Thread(target=self._server.serve_forever, name="Server Thread")
        self._server_thread.daemon = True
        self._cron = MyCron(config.CRON_BASE_DELAY)
        self._cron.add_task("sim", config.CRON_SIM_INTERVAL, self._cron_fun, InnerCommands.Sim)
        self._cron.add_task("dump", config.CRON_DUMP_INTERVAL, self._cron_fun, InnerCommands.Dump)
        self._game = Game()
        self._controllers = {
            "GET": GetController(self._game),
            "POST": PostController(self._game),
        }
        self._logfiles = {}
        for name, filename in config.SERVER_LOG_FILES.items():
            self._logfiles[name] = open(config.SERVER_LOG_DIR + "/" + filename, "a", 1)  # line buffered
    
    def start(self):
        """Start server and cron threads"""
        self._server_thread.start()
        self._cron.start()
        self._log_sys("Listening at " + self._address[0] + ":" + str(self._address[1]) + "...")
    
    def serve_forever(self):
        """Main loop of core server"""
        try:
            while True:
                try:
                    request = self._request_queue.get(True, 4)
                except queue.Empty:
                    continue
                if "inner_command" in request:
                    self._execute_inner_command(request["inner_command"])
                else:
                    response = self._execute_client_request(request["method"],
                                                            request["command"],
                                                            request["arguments"],
                                                            request["auth_cookie_value"],
                                                            request["client_ip"])
                    request["response_queue"].put(response)
                    del request["response_queue"]  # might be unnecessary
                self._request_queue.task_done()
        except (KeyboardInterrupt, SystemExit):
            self._log_sys("Shutting down...")
        finally:
            self._server.shutdown()  # shutdown http server
            self._shutdown()  # shutdown zandagort server
    
    def _shutdown(self):
        """Close logfiles"""
        self._log_sys("Shut down.")
        for _, logfile in self._logfiles.items():
            logfile.close()
    
    def _execute_inner_command(self, command):
        """Execute inner commands like Sim or Dump"""
        if command == InnerCommands.Sim:
            self._game.sim()
            self._log_sys("[" + str(command) + "] game time = " + str(self._game.get_time()))
        elif command == InnerCommands.Dump:
            self._log_sys("[" + str(command) +  "] Dumping...")
            # TODO: add dump feature
            self._log_sys("[" + str(command) +  "] Dumped.")
        else:
            self._log_sys("[" + str(command) + "] Unknown command")
    
    def _execute_client_request(self, method, command, arguments, auth_cookie_value, client_ip):
        """Execute commands sent by clients"""
        current_user = self._game.auth.get_user_by_auth_cookie(auth_cookie_value)
        if current_user is None:
            auth_cookie_value, current_user = self._game.auth.create_new_session()
        self._game.auth.renew_session(auth_cookie_value)
        
        request_string = create_request_string(method, command, arguments, client_ip)
        if method not in ["GET", "POST"]:
            self._log_error(request_string + " ! Unknown method")
            return {"error": "Unknown method", "auth_cookie_value": auth_cookie_value}
        try:
            controller_function = getattr(self._controllers[method], command)
        except AttributeError:
            self._log_error(request_string + " ! Unknown command")
            return {"error": "Unknown command", "auth_cookie_value": auth_cookie_value}
        if arguments == ErrorCodes.ArgumentSyntaxError:
            self._log_error(request_string + " ! Syntax error in arguments")
            return {"error": "Syntax error in arguments", "auth_cookie_value": auth_cookie_value}
        
        if not getattr(controller_function, "is_public", False):
            if self._game.auth.is_guest(current_user):
                self._log_error(request_string + " ! Access denied. You have to login.")
                return {"error": "Access denied. You have to login.", "auth_cookie_value": auth_cookie_value}
        
        # TODO: token check for post functions
        
        self._controllers[method].current_user = current_user
        self._controllers[method].auth_cookie_value = auth_cookie_value
        try:
            response = controller_function(**arguments)
        except Exception:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            self._log_error(request_string + " ! " + str(exc_type.__name__) + ": " + str(exc_value))
            trace_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
            for trace_line in trace_lines:
                self._log_error("    " + trace_line.rstrip(), raw=True)
            return {"error": str(exc_type.__name__) + ": " + str(exc_value), "auth_cookie_value": auth_cookie_value}
        
        self._log_access(request_string)
        return {"response": response, "auth_cookie_value": auth_cookie_value}
    
    def _cron_fun(self, command):
        """Simple helper function for cron thread"""
        self._request_queue.put({
            "inner_command": command
        })
    
    def _log(self, logtype, message, raw=False):
        """General log function for file and stdout"""
        if not raw:
            message = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + message
        if config.SERVER_LOG_STDOUT.get(logtype, "False"):
            if not raw:
                print ("[" + logtype.upper() + "] " + message)
            else:
                print (message)
        if logtype in self._logfiles:
            self._logfiles[logtype].write(message + "\n")
    
    def _log_access(self, message, raw=False):
        """Wrapper for access log"""
        self._log("access", message, raw)
    
    def _log_error(self, message, raw=False):
        """Wrapper for error log"""
        self._log("error", message, raw)
    
    def _log_sys(self, message, raw=False):
        """Wrapper for sys log"""
        self._log("sys", message, raw)


def main():
    """Create, start and run Zandagort Server"""
    
    print ("Launching Zandagort Server...")
    try:
        server = ZandagortServer(config.SERVER_HOST, config.SERVER_PORT)
    except socket_error as serr:
        if serr.errno == errno.EACCES:
            print ("[ERROR] port " + str(config.SERVER_PORT) + " already used by some other service.")
            print ("Change it in config.py")
            return
        else:
            raise
    server.start()
    print ("Zandagort Server launched.")
    server.serve_forever()  # blocking call
    print ("Zandagort Server shut down.")


if __name__ == "__main__":
    main()
