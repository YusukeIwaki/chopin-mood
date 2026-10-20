"""Studio constants: versions, limits, allowlists (single source of truth)."""
from __future__ import annotations

OWNER = "YusukeIwaki"
DEFAULT_BRANCH = "main"
BOT_LOGINS = {"github-actions[bot]"}

CODEX_VERSION = "0.144.6"
CODEX_MODEL = "gpt-6.1-sol"
CODEX_EFFORT = "xhigh"
NODE_VERSION = "24"
MUSE_MODEL = "muse-wonder-1.0-contributor"
MUSE_EFFORT = "max"
MUSE_MAX_STEPS = 80

CANDIDATE_MIN_SEC = 60.0
CANDIDATE_MAX_SEC = 120.0
MAX_CANDIDATES = 3
MIN_SCENE_SEC = 6.0
FPS_NUM, FPS_DEN = 30, 1

MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_DURATION_SEC = 900.0
MAX_SAMPLE_RATE = 96000
MAX_CHANNELS = 2
DOWNLOAD_TIMEOUT_SEC = 120

ARTIFACT_RETENTION_DAYS = 90
RELEASE_TAG_PREFIX = "studio-pr-"
PRODUCTION_BRANCH_PREFIX = "studio/issue-"

# GitHub's own upload allowances (Oct 2026 docs); the 512 MB controller cap
# above is defence-in-depth, NOT the GitHub allowance.
GITHUB_ATTACHMENT_DOC_LIMITS = {
    "audio_wav_mp3_mb": 25,
    "video_free_mb": 10,
    "video_paid_mb": 100,
}

TRUSTED_ATTACHMENT_HOSTS = {"github.com"}
TRUSTED_ATTACHMENT_PATH_PREFIX = "/user-attachments/"
# Precise hosts a GitHub user-attachment redirect may land on. Arbitrary
# https hosts, IP literals, private/link-local addresses and non-443
# ports are all rejected (parent audit: evil.example, 169.254.169.254).
TRUSTED_REDIRECT_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "media.githubusercontent.com",
    "github-cloud.githubusercontent.com",
}
# GitHub user-asset S3 buckets look like
# github-production-user-asset-<hex>.s3.amazonaws.com
TRUSTED_REDIRECT_HOST_SUFFIXES = (".s3.amazonaws.com",)
TRUSTED_REDIRECT_HOST_PREFIXES = ("github-production-user-asset-",)
# Signed storage hosts an authenticated Actions-artifact redirect may use
# (Authorization is always dropped before following these).
ARTIFACT_STORAGE_SUFFIXES = (".blob.core.windows.net", ".s3.amazonaws.com")

WORKFLOW_PATH = ".github/workflows/studio-production.yml"
WORKFLOW_NAME = "Studio production"

ALLOWED_PRODUCTION_SUFFIXES = {".json", ".md", ".txt", ".html"}
ALLOWED_PRODUCTION_NAMES = {
    "brief.json", "storyboard.json", "plan.json", "qa.json",
    "provenance.json", "timeline.json", "timeline.html", "review.json", "notes.md",
}
# Stable sources committed atomically; their hashes never embed the final
# Git commit SHA (that binding lives only in the artifact receipt.json).
COMMITTED_PRODUCTION_FILES = (
    "brief.json", "storyboard.json", "plan.json", "qa.json",
    "provenance.json", "timeline.json",
)
MAX_PRODUCTION_FILE_BYTES = 256 * 1024
MAX_ARTIFACT_FILE_BYTES = 512 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 1024 * 1024 * 1024
EXPECTED_ARTIFACT_FILES = {"video.mp4", "plan.json", "qa.json",
                           "storyboard.json", "brief.json",
                           "provenance.json", "timeline.json", "timeline.html",
                           "contact-sheet.jpg", "hashes.json", "review.json",
                           "receipt.json"}
RELEASE_ASSET_FILES = ("video.mp4", "plan.json", "qa.json", "storyboard.json",
                       "brief.json", "provenance.json", "timeline.json",
                       "timeline.html", "receipt.json")

AUTH_SECRETS = ("CODEX_ENC_APP_ID", "CODEX_ENC_APP_PRIVATE_KEY",
                "CODEX_ENC_PASSPHRASE", "MUSE_ENC_PASSPHRASE")
PERIOD_DIRECTIONS = {
    "early": "aspiration for professional performance",
    "middle": "concert presence and expressiveness",
    "late": "healing",
}
