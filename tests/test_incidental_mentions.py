from contract_ipo_monitor.sources.discourse import CompanyWatch, matching_companies
from contract_ipo_monitor.universe import default_universe


def test_social_profile_url_does_not_establish_company_opinion():
    watches = (CompanyWatch("Meta Platforms", aliases=("Facebook", "$META")),)
    text = "The gold company promoter is successful: https://www.facebook.com/promoter and www.example.org/$META/profile"
    assert matching_companies(text, watches) == ()
    assert matching_companies(text + " Meta Platforms shares are overvalued.", watches) == ("Meta Platforms",)
    assert matching_companies("I am bullish on $META earnings.", watches) == ("Meta Platforms",)


def test_subsidiary_navigation_does_not_assign_parent_investment_tone():
    meta = next(item for item in default_universe() if item.symbol == "META")
    watches = (CompanyWatch(meta.name, aliases=meta.aliases),)
    text = "Shanti Gold: Website of the company Linked In Instagram Background and Promoters. Manoj Jain Social Media https://www.instagram.com/mj7776_/"
    assert matching_companies(text, watches) == ()
    assert matching_companies("Facebook advertising prices affect customers.", watches) == ("Meta Platforms",)


def test_corrected_identity_rules_revalidate_checkpoint_content_without_erasing_receipts(tmp_path):
    from datetime import UTC, datetime
    from contract_ipo_monitor.sources.discourse import DiscourseBatch, DiscourseEvidence
    from test_research_integration import service
    monitor = service(tmp_path)
    now = datetime(2026, 10, 5, 19, tzinfo=UTC)
    evidence = DiscourseEvidence("old-attribution", "forum", "https://example.org/gold", "forum:author:one",
                                "Gold company", "An excellent successful company with strong profits. Instagram promoter navigation.",
                                "forum_post", ("Meta Platforms",), now, published_at=now)
    monitor.research.record_batch(DiscourseBatch((evidence,), ()))
    meta = next(item for item in monitor.create_report()["sentiment"] if item["company_name"] == "Meta Platforms")
    assert meta["evidence_count"] == meta["scored_count"] == 0
    assert monitor.research.records()[0].company_names == ("Meta Platforms",)
