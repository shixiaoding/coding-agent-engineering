#!/usr/bin/env ruby
# frozen_string_literal: true

require "pathname"
require "yaml"

class RulesValidator
  LEVELS = %w[MUST SHOULD].freeze
  LEARNING_STATUSES = %w[待确认 已采纳 已拒绝].freeze
  ID_PATTERN = /\A[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+\z/
  SKILL_NAME_PATTERN = /\A[a-z0-9]+(?:-[a-z0-9]+)*\z/
  RULE_HEADING_PATTERN = /^## \[([^\]]+)\] (\S+)[ \t]*$/
  LEARNING_HEADING_PATTERN = /^### \[([^\]]+)\] (\S+)[ \t]*$/
  ENABLED_SKILL_PATTERN = /^- `([^`]+)`(?:：|:)/
  SKILL_REFERENCE_PATTERN = /\]\((references\/[^)#]+\.md)(?:#[^)]+)?\)/

  attr_reader :formal_rule_count, :learning_record_count

  def initialize(rules_dir, skills_dir = nil)
    @rules_dir = Pathname(rules_dir).expand_path
    @skills_dir = skills_dir ? Pathname(skills_dir).expand_path : @rules_dir.join("..", "skills").cleanpath
    @errors = []
    @formal_rules = []
    @learning_records = []
  end

  def validate
    return ["规则目录不存在：#{@rules_dir}"] unless @rules_dir.directory?

    entry = @rules_dir.join("coding-rules.md")
    learning = @rules_dir.join("coding-rules-learning.md")
    @errors << "缺少正式规则入口：coding-rules.md" unless entry.file?
    @errors << "缺少学习文件：coding-rules-learning.md" unless learning.file?
    return @errors unless entry.file? && learning.file?

    validate_rules_directory
    enabled_skills = parse_enabled_skills(entry)
    validate_formal_document(entry)
    validated_skills = enabled_skills.dup
    validated_skills << "coding-rules-loop" if @skills_dir.join("coding-rules-loop").directory?
    validate_skills(validated_skills.uniq)
    validate_learning(learning)
    validate_duplicate_ids

    @formal_rule_count = @formal_rules.length
    @learning_record_count = @learning_records.length
    @errors
  end

  private

  def validate_rules_directory
    expected = %w[coding-rules-learning.md coding-rules.md]
    actual = @rules_dir.glob("*.md").map { |file| file.basename.to_s }.sort
    extra = actual - expected
    @errors << "rules 目录只保留正式入口和 learning，发现：#{extra.join(', ')}" unless extra.empty?
  end

  def parse_enabled_skills(entry)
    content = entry.read
    section = content[/^## 启用的专业 Skill\s*$\n(.*?)(?=^## |\z)/m, 1]
    unless section
      @errors << "coding-rules.md 缺少“启用的专业 Skill”章节"
      return []
    end

    names = section.lines.each_with_object([]) do |line, result|
      match = line.match(ENABLED_SKILL_PATTERN)
      result << match[1] if match
    end
    @errors << "至少需要启用一个专业 Skill" if names.empty?
    if names.uniq.length != names.length
      duplicates = names.tally.select { |_name, count| count > 1 }.keys
      @errors << "启用的专业 Skill 存在重复：#{duplicates.join(', ')}"
    end
    names
  end

  def validate_skills(skill_names)
    skill_names.each do |skill_name|
      unless skill_name.match?(SKILL_NAME_PATTERN)
        @errors << "非法 Skill 名称：#{skill_name.inspect}"
        next
      end

      skill_file = @skills_dir.join(skill_name, "SKILL.md")
      unless skill_file.file?
        @errors << "正式入口引用不存在的 Skill：#{skill_name}"
        next
      end

      validate_skill_frontmatter(skill_name, skill_file)
      validate_formal_document(skill_file)
      validate_skill_references(skill_file)
    end
  end

  def validate_skill_frontmatter(skill_name, skill_file)
    content = skill_file.read
    match = content.match(/\A---\s*\n(.*?)^---\s*$\n/m)
    unless match
      @errors << "#{relative(skill_file)} 缺少 YAML frontmatter"
      return
    end

    data = YAML.safe_load(match[1], permitted_classes: [], permitted_symbols: [], aliases: false)
    unless data.is_a?(Hash)
      @errors << "#{relative(skill_file)} frontmatter 必须是 mapping"
      return
    end

    @errors << "#{relative(skill_file)} 的 name 必须等于目录名 #{skill_name}" unless data["name"] == skill_name
    description = data["description"]
    @errors << "#{relative(skill_file)} 缺少非空 description" unless description.is_a?(String) && !description.strip.empty?
  rescue Psych::SyntaxError => e
    @errors << "#{relative(skill_file)} frontmatter 解析失败：#{e.problem}"
  end

  def validate_formal_document(file)
    content = file.read
    @errors << "#{relative(file)} 不应包含逐规则 YAML 代码块" if content.match?(/^```ya?ml\s*$/)
    @errors << "#{relative(file)} 混入 learning 状态标题" if content.match?(LEARNING_HEADING_PATTERN)

    content.each_line.with_index(1) do |line, line_number|
      next unless line.start_with?("## [")

      match = line.match(RULE_HEADING_PATTERN)
      unless match
        @errors << "#{relative(file)}:#{line_number}: 非法正式规则标题"
        next
      end

      level = match[1]
      id = match[2]
      @errors << "#{relative(file)}:#{line_number}: 非法规则级别 #{level.inspect}" unless LEVELS.include?(level)
      @errors << "#{relative(file)}:#{line_number}: 非法规则 ID #{id.inspect}" unless id.match?(ID_PATTERN)
      @formal_rules << { id: id, file: file, line: line_number }
    end
  end

  def validate_skill_references(skill_file)
    skill_file.read.scan(SKILL_REFERENCE_PATTERN).flatten.uniq.each do |reference|
      reference_path = Pathname(reference)
      if reference_path.absolute? || reference_path.each_filename.to_a.include?("..")
        @errors << "#{relative(skill_file)} 包含非法 reference：#{reference}"
        next
      end

      target = skill_file.dirname.join(reference_path)
      @errors << "#{relative(skill_file)} 引用不存在的 reference：#{reference}" unless target.file?
    end
  end

  def validate_learning(file)
    content = file.read
    @errors << "coding-rules-learning.md 不得包含正式规则标题" if content.match?(RULE_HEADING_PATTERN)

    content.each_line.with_index(1) do |line, line_number|
      next unless line.start_with?("### [")

      match = line.match(LEARNING_HEADING_PATTERN)
      unless match
        @errors << "coding-rules-learning.md:#{line_number}: 非法 learning 标题"
        next
      end

      status = match[1]
      id = match[2]
      @errors << "coding-rules-learning.md:#{line_number}: 非法 learning 状态 #{status.inspect}" unless LEARNING_STATUSES.include?(status)
      @errors << "coding-rules-learning.md:#{line_number}: 非法规则 ID #{id.inspect}" unless id.match?(ID_PATTERN)
      @learning_records << { id: id, status: status, line: line_number }
    end

    @errors << "coding-rules-learning.md 至少需要一条结构化记录" if @learning_records.empty?
  end

  def validate_duplicate_ids
    @formal_rules.group_by { |rule| rule[:id] }.each do |id, rules|
      next if rules.length == 1
      locations = rules.map { |rule| "#{relative(rule[:file])}:#{rule[:line]}" }.join(", ")
      @errors << "重复正式规则 ID #{id}：#{locations}"
    end
  end

  def relative(file)
    file.relative_path_from(@rules_dir.join("..").cleanpath).to_s
  rescue ArgumentError
    file.to_s
  end
end

if $PROGRAM_NAME == __FILE__
  repo_root = Pathname(__dir__).join("..").expand_path
  rules_dir = Pathname(ARGV.fetch(0, repo_root.join("rules")))
  skills_dir = Pathname(ARGV.fetch(1, repo_root.join("skills")))
  validator = RulesValidator.new(rules_dir, skills_dir)
  errors = validator.validate

  if errors.empty?
    puts "规则校验通过：#{validator.formal_rule_count} 条正式规则，#{validator.learning_record_count} 条 learning 记录"
    exit 0
  end

  warn "规则校验失败（#{errors.length} 项）："
  errors.each { |error| warn "- #{error}" }
  exit 1
end
