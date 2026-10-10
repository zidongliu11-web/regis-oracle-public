"""Disclosed runtime guard: failed receive frames cannot reuse the connection."""
import time
class GuardedSocket:
    def __init__(self,sock,emit,*,byte_cap=2097152,call_cap=512,response_seconds=60.0):
        if byte_cap!=2097152 or call_cap!=512 or response_seconds!=60.0:raise ValueError("transport_bounds_changed")
        self._sock=sock;self._emit=emit;self._byte_cap=byte_cap;self._call_cap=call_cap;self._seconds=response_seconds;self._unusable=False
        self.stats={"send_calls":0,"sent_bytes":0,"recv_calls":0,"received_bytes":0,"EOF_observed":False,"partial_send_observed":False,"receive_deadline_exceeded":False,"connection_unusable":False,"connection_unusable_reason":None,"blocked_send_attempts":0,"close_error":False,"complete_response_certified":False,"automatic_retries":0}
        self._started=None
    def invalidate(self,reason="source_response_not_passed"):
        if reason not in ("source_response_not_passed","send_failed","partial_send","receive_failed","receive_deadline","receive_call_budget","peer_closed","receive_byte_cap","invalid_socket_mode"):raise ValueError("transport_invalidation_reason")
        if not self._unusable:
            self._unusable=True;self.stats["connection_unusable"]=True;self.stats["connection_unusable_reason"]=reason
            try:self._sock.close()
            except OSError:self.stats["close_error"]=True
        self._emit(dict(self.stats))
    def send(self,payload,*args,**kwargs):
        if self._unusable:
            self.stats["blocked_send_attempts"]+=1;self._emit(dict(self.stats))
            raise ConnectionError("transport_unusable_no_message_sent")
        self.stats["send_calls"]+=1;self._started=time.monotonic()
        # A sent request owns the connection until its response is consumed.
        # Telemetry failure after send must therefore poison it as well.
        try:
            sent=self._sock.send(payload,*args,**kwargs)
            self.stats["sent_bytes"]+=sent;self._emit(dict(self.stats))
            if sent!=len(payload):
                self.stats["partial_send_observed"]=True;self.invalidate("partial_send")
                raise ConnectionError("transport_partial_send_no_replay")
            return sent
        except Exception:
            self.invalidate("send_failed");raise
    def _remaining(self):
        remaining=self._seconds-(time.monotonic()-self._started)
        if remaining<=0:
            self.stats["receive_deadline_exceeded"]=True;self.invalidate("receive_deadline")
            raise TimeoutError("transport_receive_deadline_exceeded")
        return remaining
    def recv(self,size,*args,**kwargs):
        if self._unusable:raise ConnectionError("transport_unusable_no_receive")
        if self._started is None:raise RuntimeError("transport_recv_before_send")
        try:
            if self.stats["recv_calls"]>=self._call_cap:
                self.invalidate("receive_call_budget");raise TimeoutError("transport_receive_call_budget")
            self._remaining();self.stats["recv_calls"]+=1;self._emit(dict(self.stats))
            remaining=self._remaining();previous=self._sock.gettimeout()
            if previous is not None and previous<=0:
                self.invalidate("invalid_socket_mode");raise ValueError("transport_nonblocking_not_supported")
            self._sock.settimeout(min(previous,remaining) if previous is not None else remaining)
            try:chunk=self._sock.recv(size,*args,**kwargs)
            finally:self._sock.settimeout(previous)
            self.stats["received_bytes"]+=len(chunk);self.stats["EOF_observed"]=not chunk;self._emit(dict(self.stats))
            self._remaining()
            if not chunk:
                self.invalidate("peer_closed");raise ConnectionError("transport_peer_closed_before_decoder")
            if self.stats["received_bytes"]>self._byte_cap:
                self.invalidate("receive_byte_cap");raise ValueError("transport_receive_byte_cap")
            return chunk
        except Exception:
            self.invalidate("receive_failed");raise
    def close(self):return self._sock.close()
