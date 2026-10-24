"""Product identification and evidence-first research planning.

No claims of TikTok virality are made without externally retrieved video data.
"""
import re


def identify_product(product):
    title = (product.get("title") or "").strip()
    description = re.sub(r"<[^>]+>", " ", product.get("body_html") or "")
    description = " ".join(description.split())[:1500]
    image = ((product.get("images") or [{}])[0] or {}).get("src")
    words = re.findall(r"[a-zA-Z0-9]+", title.lower())
    ignored = {"new", "viral", "sale", "official", "best", "the", "for", "with"}
    keywords = [w for w in words if w not in ignored]
    queries = list(dict.fromkeys([
        title,
        " ".join(keywords),
        " ".join(keywords[:4]) + " review" if keywords else "",
        " ".join(keywords[:4]) + " tiktok" if keywords else "",
    ]))
    return {
        "title": title,
        "description": description,
        "image_url": image,
        "search_queries": [q for q in queries if q],
        "video_search_status": "not_connected",
        "viral_status": "unknown",
        "matched_videos": [],
        "reason": "No verified TikTok video search provider configured.",
    }


def evaluate_videos(videos, *, min_views=100000, min_like_rate=0.03):
    """Only count videos explicitly verified as the same product."""
    matched = []
    for video in videos:
        if video.get("product_match_verified") is not True:
            continue
        views = video.get("views")
        likes = video.get("likes")
        if not isinstance(views, int) or not isinstance(likes, int) or views <= 0:
            continue
        matched.append({**video, "like_rate": likes / views,
                        "viral_candidate": views >= min_views and likes / views >= min_like_rate})
    return {
        "matched_videos": matched,
        "viral_status": "evidence_found" if any(v["viral_candidate"] for v in matched)
                        else ("not_established" if matched else "unknown"),
    }
