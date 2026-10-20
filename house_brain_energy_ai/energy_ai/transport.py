"""Fixed local Supervisor route, no redirects, no equipment service endpoint."""
import json
import urllib.request
from urllib.parse import urlencode, quote
from datetime import datetime, timezone
import re
from .store import encode


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError("REDIRECT_REFUSED")


class HAClient:
    def __init__(self, token):
        if not token:
            raise ValueError("SUPERVISOR_TOKEN_REQUIRED")
        self.token=token
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        self.calls=0

    def request(self, path, payload=None):
        allowed=path=="/states" or re.fullmatch(r"/states/sensor\.house_brain_energy_ai_[a-z0-9_]+",path)
        if not allowed or "?" in path or ".." in path:
            raise ValueError("ENDPOINT_REFUSED")
        if payload is not None and path=="/states":
            raise ValueError("WRITE_REFUSED")
        return self._exchange(path,payload)

    def _exchange(self,path,payload=None):
        request=urllib.request.Request("http://supervisor/core/api"+path,
            data=encode(payload).encode() if payload is not None else None,
            headers={"Authorization":"Bearer "+self.token,"Content-Type":"application/json"})
        self.calls+=1
        with self.opener.open(request,timeout=10) as response:
            raw=response.read(4_000_001)
            if len(raw)>4_000_000:
                raise ValueError("RESPONSE_TOO_LARGE")
            return json.loads(raw)

    def history(self,entity,start,end):
        """Supported Recorder history GET; one entity/day, full historical units."""
        if not re.fullmatch(r"sensor\.[a-z0-9_]+",entity) or not 0<end-start<=86400:
            raise ValueError("HISTORY_SCOPE_REFUSED")
        stamp=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
        query=urlencode({'filter_entity_id':entity,'end_time':stamp(end)})
        result=self._exchange('/history/period/'+quote(stamp(start),safe='')+'?'+query)
        if not isinstance(result,list) or len(result)>1:
            raise ValueError("HISTORY_SHAPE")
        rows=result[0] if result else []
        if len(rows)>10000:
            raise ValueError("HISTORY_ROW_LIMIT")
        return rows

    def states(self):
        result=self.request("/states")
        if not isinstance(result,list):
            raise ValueError("STATE_LIST_REQUIRED")
        return {r["entity_id"]:r for r in result if isinstance(r,dict) and "entity_id" in r}

    def publish(self, status):
        # One bounded status entity; state POST is entity presentation, not a service call.
        return self.request("/states/sensor.house_brain_energy_ai_status",{
            "state":status["state"],"attributes":{
                "friendly_name":"House Brain Energy AI", "version":status["version"],
                "mode":status["mode"],"physical_actions":False,"last_cycle":status["at"],
                "data_quality":status["data_quality"],"model_status":status["model_status"],
                "plan_status":status["plan_status"],"detail":"Open Energy AI ingress for evidence"}})
