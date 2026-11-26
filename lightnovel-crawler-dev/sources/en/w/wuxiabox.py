import logging

from lncrawl.templates.novelmtl import NovelMTLTemplate

logger = logging.getLogger(__name__)


class Wuxiabox(NovelMTLTemplate):
    can_search = True
    has_mtl = True
    has_manga = False
    base_url = ["https://www.wuxiabox.com/"]
