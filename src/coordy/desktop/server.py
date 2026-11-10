"""Point this PC at the company AI server chosen in Settings (private networks only)."""
from ..llm import is_local
from ..remote import is_private_host


def apply_server(settings, preferences):
    """Overlay desktop server choices on config.toml settings. Returns the same object."""
    for label, url in (("Minutes AI server", preferences.llm_host), ("Speech server", preferences.speech_server)):
        if url and not is_local(url) and not is_private_host(url):
            raise ValueError(f"{label} {url} is not on a private company network or could not be resolved. "
                             "Use the server's internal address, for example http://10.0.0.20:11434.")
    if preferences.llm_host:
        settings.llm.host = preferences.llm_host.rstrip("/")
        settings.privacy.allow_remote_llm = True  # verified private above
    if preferences.speech_server:
        settings.whisper.server = preferences.speech_server.rstrip("/")
        settings.whisper.server_model = preferences.speech_server_model
    return settings


def describe(settings):
    """Short human labels for where speech and minutes run."""
    speech = "Company server" if settings.whisper.server else "This PC"
    minutes = "This PC" if is_local(settings.llm.host) else "Company server"
    return speech, minutes
