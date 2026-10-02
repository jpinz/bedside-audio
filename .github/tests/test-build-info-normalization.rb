require "open3"
require "tempfile"
require "yaml"

workflow = YAML.safe_load(
  File.read(File.expand_path("../workflows/build-app.yaml", __dir__)),
  aliases: true,
)
step = workflow.fetch("jobs").fetch("prepare").fetch("steps").find do |candidate|
  candidate["name"] == "Normalize app information"
end
raise "Normalize app information step is missing" unless step

quoted_outputs = {
  "${{ steps.info.outputs.description }}" => '"Voice PE hardware controls"',
  "${{ steps.info.outputs.image }}" => '"ghcr.io/jpinz/bedside-audio"',
  "${{ steps.info.outputs.name }}" => '"Bedside hardware (Music Assistant)"',
  "${{ steps.info.outputs.url }}" => '"https://github.com/jpinz/bedside-audio"',
  "${{ steps.info.outputs.version }}" => '"0.11.0"',
}

environment = (step["env"] || {}).to_h do |name, value|
  [name, quoted_outputs.fetch(value, value)]
end
script = step.fetch("run").dup
quoted_outputs.each { |expression, value| script.gsub!(expression, value) }

Tempfile.create("builder-output") do |output|
  environment["GITHUB_OUTPUT"] = output.path
  stdout, stderr, status = Open3.capture3(
    environment,
    "bash",
    "-euo",
    "pipefail",
    "-c",
    script,
  )
  abort "#{stdout}#{stderr}" unless status.success?

  values = File.readlines(output.path, chomp: true).to_h do |line|
    line.split("=", 2)
  end
  expected = {
    "image_name" => "bedside-audio",
    "registry_prefix" => "ghcr.io/jpinz",
    "version" => "0.11.0",
  }
  actual = values.slice(*expected.keys)
  abort "expected #{expected.inspect}, got #{actual.inspect}" unless actual == expected
  abort "normalized outputs retain quote characters" if values.values.any? { |value| value.include?('"') }

  puts "normalized image_name=#{values.fetch("image_name")}"
  puts "normalized registry_prefix=#{values.fetch("registry_prefix")}"
  puts "normalized version=#{values.fetch("version")}"
end
