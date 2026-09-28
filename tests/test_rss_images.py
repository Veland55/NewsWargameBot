"""Отбор картинок тела статьи (rss._body_images) на разметке, повторяющей
реальные страницы. Сеть не трогаем — HTML задан строкой."""
from __future__ import annotations

from bot import rss

ARTICLE = "https://store.catalystgamelabs.com/blogs/news/the-rules-of-warfare"

# Сокращённая разметка блога Shopify (store.catalystgamelabs.com): текст статьи
# в div.rte, картинка в тексте обёрнута ссылкой на другую статью; следом —
# карточка «Next Blog», карусель «Our Latest Articles» со стрелками-SVG и
# слайдер товаров «You May Also Like».
SHOPIFY_PAGE = """
<div class="article-template__hero-adapt media">
  <img class="featured-image" src="//store.catalystgamelabs.com/cdn/shop/articles/cover.jpg?v=1">
</div>
<div class="article-template__content content-box page-width rte">
  <p>Now coming to general release...</p>
  <div style="display: flex;"><img src="https://cdn.shopify.com/s/files/1/files/image4.png?v=1"></div>
  <p>Read Keith's article.</p>
  <div style="display: flex;"><a href="https://www.catalystgamelabs.com/news/further-notes" target="_blank"><img src="https://cdn.shopify.com/s/files/1/files/BT_Core_Rulebook_NL.jpg?v=1"></a></div>
  <p>We'll see you on the field.</p>
</div>
<div class="next-pre-blog"><div class="next-blog-card">
  <a href="/blogs/news/the-more-things-change"><img src="//store.catalystgamelabs.com/cdn/shop/articles/SotA.jpg?v=1"></a>
</div></div>
<section class="shopify-section">
  <link href="//store.catalystgamelabs.com/cdn/shop/t/33/assets/section-latest-articles.css" rel="stylesheet">
  <div class="section-articles">
    <div class="main-bg"><img src="//store.catalystgamelabs.com/cdn/shop/files/blogs-bg.png?v=1"></div>
    <div class="prev"><img src="//store.catalystgamelabs.com/cdn/shop/t/33/assets/left-arrow.svg?v=1"></div>
    <h2>Our Latest Articles</h2>
  </div>
</section>
<section class="shopify-section"><div class="featured-products">
  <div class="pro-slider"><div class="product-card"><div class="img-box">
    <img src="//store.catalystgamelabs.com/cdn/shop/files/chaos_campaign.jpg?v=1&width=533">
  </div></div></div>
</div></section>
"""


def test_shopify_blog_keeps_only_article_images():
    assert rss._body_images(SHOPIFY_PAGE, ARTICLE, None) == [
        "https://store.catalystgamelabs.com/cdn/shop/articles/cover.jpg?v=1",
        "https://cdn.shopify.com/s/files/1/files/image4.png?v=1",
        "https://cdn.shopify.com/s/files/1/files/BT_Core_Rulebook_NL.jpg?v=1",
    ]


def test_linked_image_outside_article_body_is_still_dropped():
    page = ('<div class="entry-content"><p>Text</p></div>'
            '<a href="/other-post"><img src="https://example.com/wp-content/uploads/2026/09/other.jpg"></a>')
    assert rss._body_images(page, "https://example.com/post", None) == []


def test_article_body_span_handles_nested_divs():
    page = '<div class="rte"><div><div>x</div></div><p>y</p></div><div>after</div>'
    start, end = rss._article_body_span(page)
    assert page[start:end + len("</div>")] == '<div class="rte"><div><div>x</div></div><p>y</p></div>'
