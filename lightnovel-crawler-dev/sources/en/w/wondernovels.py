# -*- coding: utf-8 -*-
from lncrawl.templates.wordpress import WordpressTemplate


class WonderNovels(WordpressTemplate):
    can_search = True
    base_url = "https://wondernovels.com/"
    madara_body_from_paragraphs = True
