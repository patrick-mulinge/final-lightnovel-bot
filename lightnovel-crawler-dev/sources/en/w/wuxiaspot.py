from lncrawl.templates.novelmtl import NovelMTLTemplate


class WuxiaSpotCrawler(NovelMTLTemplate):
    can_search = True
    has_mtl = False
    has_manga = False
    base_url = "https://www.wuxiaspot.com/"
