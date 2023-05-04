from typing import Optional


class MixpanelWrapper:
    def __init__(self, token: Optional[str] = None):
        if token is not None:
            import mixpanel
            from mixpanel_async import AsyncBufferedConsumer
            self.mxp = mixpanel.Mixpanel(token, consumer=AsyncBufferedConsumer())
        else:
            self.mxp = None

    def track(self, distinct_id, event_name, properties):
        if self.mxp is not None:
            self.mxp.track(distinct_id, event_name, properties)
        else:
            pass

    def people_set(self, distinct_id, properties):
        if self.mxp is not None:
            self.mxp.people_set(distinct_id, properties)
        else:
            pass