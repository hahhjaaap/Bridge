"""Single-flight MCP child recovery. Never replay a request after dispatch."""
from __future__ import annotations
import copy
import json
import threading
import time


class RecoveringChild:
    def __init__(self, factory, name):
        self.factory=factory;self.name=name;self.child=factory()
        self.lock=threading.RLock();self.closed=False;self.initialization=None
        self.schema=None;self.generation=0;self.attempts=[];self.next_attempt=0.;self.last_error=''

    @property
    def process(self):return self.child.process

    def start(self):
        with self.lock:
            if self.closed:raise RuntimeError('MCP child is closed')
            self.child.start();self.generation=1

    def _alive(self):
        p=self.child.process
        return p is not None and p.poll() is None and not self.child.terminated.is_set()

    def _ready(self):
        with self.lock:
            if self.closed:raise RuntimeError('MCP child is closed')
            if self._alive():return self.child
            if self.initialization is None:raise RuntimeError(self.name+' MCP backend not initialized')
            now=time.monotonic();self.attempts=[t for t in self.attempts if now-t<60]
            if now<self.next_attempt or len(self.attempts)>=3:
                raise RuntimeError(self.name+' backend recovery is cooling down; no request was sent')
            self.attempts.append(now)
            replacement=self.factory()
            try:
                replacement.start()
                reply=replacement.request('initialize',copy.deepcopy(self.initialization),timeout=20)
                if reply.get('error'):raise RuntimeError('Recovery initialization rejected')
                replacement.notification({'jsonrpc':'2.0','method':'notifications/initialized'})
                listing=replacement.request('tools/list',timeout=20)
                if listing.get('error') or not isinstance(listing.get('result',{}).get('tools'),list):
                    raise RuntimeError('Recovery tool discovery failed')
                fingerprint=json.dumps(listing['result']['tools'],sort_keys=True,separators=(',',':'))
                if self.schema is not None and fingerprint!=self.schema:
                    raise RuntimeError('Backend tool schema changed; refresh plugin before continuing')
            except BaseException as exc:
                replacement.close();self.last_error=type(exc).__name__
                self.next_attempt=time.monotonic()+min(8,2**len(self.attempts))
                raise
            old=self.child;self.child=replacement;self.generation+=1;self.last_error='';self.next_attempt=0
            old.close()
            return replacement

    def request(self,method,params=None,timeout=90):
        if method=='initialize':
            with self.lock:
                if self.closed:raise RuntimeError('MCP child is closed')
                reply=self.child.request(method,params,timeout=timeout)
                if not reply.get('error'):self.initialization=copy.deepcopy(params or {})
                return reply
        # Revocation/shutdown cleanup must never resurrect a dead Companion.
        if method in ('udb/close-sessions','udb/close-desktop'):
            with self.lock:
                if self.closed or not self._alive():return {'result':{'closed':True,'sessions_lost':True}}
                child=self.child
        else:child=self._ready()
        # A timeout/EOF/write failure here may have followed a side effect.
        # Return that outcome to the caller. Only the NEXT request may recover.
        reply=child.request(method,params,timeout=timeout)
        if method=='tools/list' and not reply.get('error'):
            fingerprint=json.dumps(reply.get('result',{}).get('tools'),sort_keys=True,separators=(',',':'))
            with self.lock:
                if child is self.child:self.schema=fingerprint
        return reply

    def notification(self,message):
        with self.lock:
            if not self.closed and self._alive():self.child.notification(message)

    def status(self):
        with self.lock:
            return dict(alive=not self.closed and self._alive(),generation=self.generation,
                        recovery_attempts_last_minute=sum(time.monotonic()-t<60 for t in self.attempts),
                        last_error_kind=self.last_error,replay_enabled=False)

    def close(self):
        with self.lock:
            self.closed=True;child=self.child
        child.close()
