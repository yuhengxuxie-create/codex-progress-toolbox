"""Fixed-origin REST-only lifecycle notices when the WebSocket is disconnected."""
import json
import time
import uuid
import requests

from .feishu import FeishuSendError,FeishuSendNotSubmittedError,FeishuSendRejectedError,_IDEMPOTENCY_NAMESPACE,_TRANSIENT_FEISHU_CODES


class LifecycleRestSender:
    def __init__(self,app_id,app_secret,owner,*,session=None):
        self.app_id=app_id;self.app_secret=app_secret;self.owner=owner
        self.session=session or requests.Session()
        self.token=None;self.expires=0

    def send_text(self,text,*,idempotency_key):
        if not idempotency_key.startswith('system:'):
            raise ValueError('REST fallback only accepts lifecycle events')
        if not self.token or time.time()>=self.expires:
            try:
                response=self.session.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',json={'app_id':self.app_id,'app_secret':self.app_secret},timeout=(3,8),allow_redirects=False)
                response.raise_for_status();data=response.json()
                if data.get('code',0)!=0 or not isinstance(data.get('tenant_access_token'),str):
                    raise ValueError('token unavailable')
                self.token=data['tenant_access_token'];self.expires=time.time()+max(0,min(int(data.get('expire',3600)),3600)-60)
            except Exception as exc:
                raise FeishuSendNotSubmittedError('lifecycle_auth_unavailable') from exc
        stable=uuid.uuid5(_IDEMPOTENCY_NAMESPACE,idempotency_key).hex
        try:
            response=self.session.post('https://open.feishu.cn/open-apis/im/v1/messages',params={'receive_id_type':'open_id'},
                headers={'Authorization':'Bearer '+self.token},json={'receive_id':self.owner,'msg_type':'text','content':json.dumps({'text':text},ensure_ascii=False),'uuid':stable},timeout=(3,8),allow_redirects=False)
        except requests.ConnectTimeout as exc:
            raise FeishuSendNotSubmittedError('lifecycle_connect_timeout') from exc
        except Exception as exc:
            raise FeishuSendError('lifecycle_submission_unknown') from exc
        if getattr(response,'status_code',200)==429:
            raise FeishuSendRejectedError(code='lifecycle_rate_limited',raw_code=429,retryable=True)
        try:
            data=response.json()
        except Exception as exc:
            raise FeishuSendError('lifecycle_response_unknown') from exc
        if data.get('code')!=0:
            raw_code=data.get('code')
            raise FeishuSendRejectedError(code='lifecycle_api_rejected',raw_code=raw_code,retryable=raw_code in _TRANSIENT_FEISHU_CODES)
        message_id=data.get('data',{}).get('message_id')
        if not isinstance(message_id,str) or not message_id:
            raise FeishuSendError('lifecycle_response_missing_id')
        return message_id
