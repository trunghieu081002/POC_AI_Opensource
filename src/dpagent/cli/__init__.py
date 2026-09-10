"""dpagent command line."""
from __future__ import annotations

import click

from .. import __version__
from . import authoring, doctor, install, operate, report


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="dpagent")
def main():
    """dpagent — install and operate an open-source data stack from packs.

    \b
    Installing needs no API key. A model is involved only in `do` (routing a
    plain-language request), `synth` (drafting a pack for an unknown tool), and
    proposing a catalog entry for a failure nothing matched.
    """


# read-only
main.add_command(doctor.doctor_cmd)
main.add_command(report.info_cmd)
main.add_command(report.packs_cmd)
main.add_command(report.suites_cmd)
main.add_command(report.status_cmd)
main.add_command(report.audit_cmd)
main.add_command(report.errors_cmd)

# install
main.add_command(install.install_cmd)
main.add_command(install.spec_cmd)
main.add_command(install.do_cmd)

# operate
main.add_command(operate.verify_cmd)
main.add_command(operate.test_cmd)
main.add_command(operate.rollback_cmd)

# authoring
main.add_command(authoring.synth_cmd)
main.add_command(authoring.lint_cmd)
main.add_command(authoring.promote_cmd)


if __name__ == "__main__":
    main()
