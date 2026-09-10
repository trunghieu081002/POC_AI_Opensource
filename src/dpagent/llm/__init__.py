"""The three jobs a model is allowed to do, none of them in the execution path.

  client — provider-agnostic access, JSON-shaped replies
  router — a plain-language request onto packs and params, validated afterwards

Pack authoring (`library.synth`) and failure diagnosis also live behind `client`.
"""
