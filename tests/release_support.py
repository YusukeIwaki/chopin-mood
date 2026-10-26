"""Exercise the three real release phases with deterministic offline writing."""
from studio import publication

COPY = {"title": "Piano Performance / Quiet Recollection",
        "description": "The creator's piano performance accompanies animated scenery."}


def complete_release(controller, gh, **kwargs):
    staged = controller(gh, stage_only=True, **kwargs)
    if not staged.get("ok") or staged.get("skipped"):
        return staged
    prepared = publication.prepare(gh, staged["tag"])
    if prepared["needed"]:
        publication.publish(gh, prepared, COPY)
    return controller(gh, **kwargs)
