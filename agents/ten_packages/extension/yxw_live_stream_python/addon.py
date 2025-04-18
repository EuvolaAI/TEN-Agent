from ten import (
    Addon,
    register_addon_as_extension,
    TenEnv,
)

@register_addon_as_extension("yxw_live_stream_python")
class YxwLiveStreamExtensionAddon(Addon):
    def on_create_instance(self, ten: TenEnv, addon_name: str, context) -> None:
        from .extension import YxwLiveStreamExtension
        ten.log_info("on_create_instance")
        ten.on_create_instance_done(YxwLiveStreamExtension(addon_name), context)
