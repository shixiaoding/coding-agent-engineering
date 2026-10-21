# frozen_string_literal: true

require "minitest/autorun"
require "json"
require "pathname"
require "yaml"

require_relative "../../scripts/validate_rules"

class RulesValidatorTest < Minitest::Test
  REPO_ROOT = Pathname(__dir__).join("..", "..").expand_path
  FIXTURES = REPO_ROOT.join("tests", "fixtures", "rules")
  FIXTURE_SKILLS = FIXTURES.join("shared-skills")
  RULE_CASES = REPO_ROOT.join("tests", "fixtures", "rule-cases")

  def test_repository_rules_are_valid
    validator = RulesValidator.new(REPO_ROOT.join("rules"), REPO_ROOT.join("skills"))

    assert_empty validator.validate
    assert_equal 30, validator.formal_rule_count
    assert_equal 2, validator.learning_record_count
  end

  def test_valid_fixture
    assert_empty validate_fixture("valid")
  end

  def test_duplicate_formal_rule_id
    errors = validate_fixture("duplicate-id")

    assert errors.any? { |error| error.include?("重复正式规则 ID java.sample-rule") }
  end

  def test_invalid_rule_level
    errors = validate_fixture("invalid-enum-and-scope")

    assert errors.any? { |error| error.include?("非法规则级别 \"MAY\"") }
  end

  def test_learning_heading_cannot_enter_formal_rules
    errors = validate_fixture("learning-in-formal")

    assert errors.any? { |error| error.include?("混入 learning 状态标题") }
  end

  def test_missing_enabled_skill
    errors = validate_fixture("missing-reference")

    assert errors.any? { |error| error.include?("正式入口引用不存在的 Skill：not-found-skill") }
  end

  def test_skill_name_and_description_are_required
    errors = validate_fixture("missing-field")

    assert errors.any? { |error| error.include?("name 必须等于目录名 broken-skill") }
    assert errors.any? { |error| error.include?("缺少非空 description") }
  end

  def test_missing_skill_reference
    errors = validate_fixture("missing-resource")

    assert errors.any? { |error| error.include?("引用不存在的 reference：references/missing.md") }
  end

  def test_common_java_api_and_feign_selection_fixture
    cases = JSON.parse(FIXTURES.join("selection-cases.json").read)

    assert_equal(
      %w[common java java-architecture java-test-strategy existing-api new-api contract-change feign openapi multi-file-chain],
      cases.map { |test_case| test_case.fetch("name") }
    )
    assert_equal "compatibility-only", cases.find { |test_case| test_case["name"] == "feign" }.fetch("api_boundary")
    assert_equal [], cases.find { |test_case| test_case["name"] == "common" }.fetch("skills")
    assert cases.find { |test_case| test_case["name"] == "new-api" }.fetch("api_reference")
    assert cases.find { |test_case| test_case["name"] == "contract-change" }.fetch("api_reference")
    refute cases.find { |test_case| test_case["name"] == "existing-api" }.fetch("api_reference")
    assert cases.find { |test_case| test_case["name"] == "java-architecture" }.fetch("java_reference")
    assert cases.find { |test_case| test_case["name"] == "java-test-strategy" }.fetch("java_reference")
    refute cases.find { |test_case| test_case["name"] == "java" }.fetch("java_reference")
    assert cases.all? do |test_case|
      test_case.key?("files") && test_case.key?("change_hints") && test_case.key?("skills") &&
        test_case.key?("api_reference") && test_case.key?("java_reference")
    end
  end

  def test_no_semanticless_layer_behavior_fixture
    fixture = RULE_CASES.join("no-semanticless-layer")
    expected = JSON.parse(fixture.join("expected.json").read)
    before_controller = fixture.join("before", "OrderController.java").read
    after_controller = fixture.join("after", "OrderController.java").read

    assert_equal "coding.no-semanticless-layer", expected.fetch("rule")
    assert_equal "repairable", expected.fetch("result")
    assert_includes expected.fetch("preconditions"), "OrderSaveReq 已在请求边界完成基础校验"
    assert_includes before_controller, "req.getOrderNo(), req.getRemark()"
    assert_includes after_controller, "orderApplication.save(req)"
    assert_includes expected.fetch("forbidden"), "引入项目中不存在的 Command 分层"
  end

  def test_avoid_mechanical_line_wrap_behavior_fixture
    fixture = RULE_CASES.join("avoid-mechanical-line-wrap")
    expected = JSON.parse(fixture.join("expected.json").read)
    before_source = fixture.join("before", "StoreProgressService.java").read
    after_source = fixture.join("after", "StoreProgressService.java").read
    declaration = after_source.lines.find { |line| line.include?("queryStoreProgressByTaskAndOrganizationScope") }

    assert_equal "coding.avoid-mechanical-line-wrap", expected.fetch("rule")
    assert_equal "repairable", expected.fetch("result")
    assert_includes before_source, "StoreProgressQueryResult\n"
    assert_operator declaration.chomp.length, :>, 140
    assert_includes expected.fetch("forbidden"), "把 140 字符作为硬阻塞阈值"
  end

  private

  def validate_fixture(name)
    RulesValidator.new(FIXTURES.join(name), FIXTURE_SKILLS).validate
  end
end
