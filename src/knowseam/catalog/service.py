import base64
import hashlib
import hmac
import json
import time


class CursorStale(ValueError):
    pass


class CursorCodec:
    def __init__(self, secret: bytes):
        if len(secret) < 32:
            raise ValueError('cursor key too short')
        self.secret = secret

    def encode(self, *, scope, revision, offset, now=None):
        payload = json.dumps({'s': scope, 'r': revision, 'o': offset,
                              'exp': int(time.time() if now is None else now) + 600}, separators=(',', ':')).encode()
        signature = hmac.new(self.secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip('=')

    def decode(self, value, *, scope, revision, now=None):
        try:
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError()
            raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.new(self.secret, payload, hashlib.sha256).digest(), signature):
                raise ValueError()
            data = json.loads(payload)
            if (data['s'] != scope or data['r'] != revision or type(data['o']) is not int or data['o'] < 0
                    or data['exp'] <= (time.time() if now is None else now)):
                raise ValueError()
            return data['o']
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise CursorStale('cursor_stale') from None


def catalog_page(items, *, declared, complete, offset, limit):
    status = 'unavailable' if not declared else ('complete' if complete else 'partial')
    return {'items': items[offset:offset+limit], 'next_offset': offset+limit if offset+limit < len(items) else None,
            'catalog_status': status, 'scope': {'complete': complete if declared else None,
                                               'meaning': 'authorized_declared_source_scope'}}
