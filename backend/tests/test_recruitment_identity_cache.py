"""Public identity reuse must never reuse a complete job/profile score."""

from backend import recruitment, recruitment_organizations


def test_organization_probe_and_final_score_parse_hiring_evidence_once(monkeypatch):
    calls = []
    original = recruitment_organizations._candidates

    def collect(job):
        calls.append(job)
        return original(job)

    monkeypatch.setattr(recruitment_organizations, "_candidates", collect)
    result = recruitment._organization_assessment({
        "company": "中国电信集团总部", "title": "数据分析师",
        "hiring_unit": "中国电信河北省分公司",
    })
    assert len(calls) == 1
    assert result["level"] == "provincial_branch"
    assert result["is_group_headquarters"] is False


def test_name_cache_accounts_for_mutated_marker_sets():
    markers = {"中国银行"}
    assert not recruitment._company_matches_any("中信证券", markers)
    markers.add("中信证券")
    assert recruitment._company_matches_any("中信证券", markers)
    markers.remove("中信证券")
    assert not recruitment._company_matches_any("中信证券", markers)


def test_same_company_reassesses_changed_hiring_unit_and_keeps_previous_output_independent():
    common = {"company": "中国电信集团总部", "title": "数据产品分析师",
              "responsibilities": "负责数据产品、风险模型和商业分析，参与金融科技产品策略设计。",
              "requirements": "面向应届毕业生，金融、会计、计算机或管理专业。"}
    headquarters = recruitment.score_job(common, {})
    branch = recruitment.score_job({**common, "hiring_unit": "中国电信石家庄市分公司"}, {})
    assert headquarters["organization_assessment"]["is_group_headquarters"] is True
    assert branch["organization_assessment"]["level"] == "city_branch"
    assert branch["employer_score"] < headquarters["employer_score"]
    branch["organization_assessment"]["evidence"].clear()
    branch["job_score"] = 100
    assert recruitment.score_job(common, {}) == headquarters
