import binascii
import os
import re
import subprocess

import pexpect
import toml
import yaml

from base64 import b64decode
from datetime import datetime, timedelta
from stat import S_IREAD, S_IWRITE, S_IEXEC
from subprocess import DEVNULL
from tempfile import mkdtemp, mkstemp
from typing import Dict, List, Mapping, MutableMapping, Optional

from croniter import croniter
from github import Github, UnknownObjectException
from github.Requester import requests
from gnupg import GPG

from . import Abort, debug, info, warn, error
from .changelog import Changelog
from .git import Git


class Repo:
    """A Repo has access to its Git repository and registry metadata."""

    def __init__(
        self,
        *,
        repo: str,
        registry: str,
        token: str,
        changelog: str,
        changelog_ignore: List[str],
        ssh: bool,
        gpg: bool,
    ) -> None:
        gh = Github(token, per_page=100)
        self._repo = gh.get_repo(repo, lazy=True)
        self._registry = gh.get_repo(registry, lazy=True)
        self._token = token
        self._changelog = Changelog(self, changelog, changelog_ignore)
        self._ssh = ssh
        self._gpg = gpg
        self._git = Git(repo, token)
        self.__project: Optional[MutableMapping[str, object]] = None
        self.__registry_path: Optional[str] = None
        self.__lookback: Optional[timedelta] = None

    def _project(self, k: str) -> str:
        """Get a value from the Project.toml."""
        if self.__project is not None:
            return str(self.__project[k])
        for name in ["Project.toml", "JuliaProject.toml"]:
            path = self._git.path(name)
            if os.path.isfile(path):
                with open(path) as f:
                    self.__project = toml.load(f)
                return str(self.__project[k])
        raise Abort("Project file was not found")

    @property
    def _registry_path(self) -> Optional[str]:
        """Get the package's path in the registry repo."""
        if self.__registry_path is not None:
            return self.__registry_path
        contents = self._registry.get_contents("Registry.toml")
        registry = toml.loads(contents.decoded_content.decode())
        uuid = self._project("uuid")
        if uuid in registry["packages"]:
            self.__registry_path = registry["packages"][uuid]["path"]
            return self.__registry_path
        return None

    @property
    def _lookback(self) -> timedelta:
        """Calculate the lookback period."""
        if self.__lookback is not None:
            return self.__lookback
        default = timedelta(days=3, hours=1)
        workflows = self._git.path(".github", "workflows")
        for workflow in os.listdir(workflows):
            path = os.path.join(workflows, workflow)
            if os.path.isdir(path):
                continue
            try:
                with open(path) as f:
                    data = yaml.safe_load(f)
                for job in data["jobs"].values():
                    for step in job["steps"]:
                        if "TagBot" in step["uses"]:
                            # The "on" key parses to a boolean True. YAML is dumb.
                            cron = croniter(data[True]["schedule"][0]["cron"])
                            seconds = cron.get_next() - cron.get_prev()
                            # Rather than just using the cron interval, multiply it by 3
                            # so that random errors will get retried a couple of times.
                            # For backwards compatibility, make the default the minimum.
                            self.__lookback = max(
                                timedelta(seconds=seconds * 3, hours=1), default
                            )
                            return self.__lookback
            except:  # noqa: E722
                pass
        return default

    def _maybe_b64(self, val: str) -> str:
        """Return a decoded value if it is Base64-encoded, or the original value."""
        try:
            val = b64decode(val, validate=True).decode()
        except binascii.Error:
            pass
        return val

    def _release_exists(self, version: str) -> bool:
        """Check whether or not a GitHub release exists."""
        try:
            self._repo.get_release(version)
            return True
        except UnknownObjectException:
            return False

    def _create_release_branch_pr(self, version: str, branch: str) -> None:
        """Create a pull request for the release branch."""
        self._repo.create_pull(
            title=f"Merge release branch for {version}",
            body="",
            head=branch,
            base=self._repo.default_branch,
        )

    def _filter_map_versions(self, versions: Dict[str, str]) -> Dict[str, str]:
        """Filter out versions and convert tree SHA to commit SHA."""
        valid = {}
        for version, tree in versions.items():
            version = f"v{version}"
            sha = self._git.commit_sha_of_tree(tree)
            if not sha:
                warn(f"No matching commit was found for version {version} ({tree})")
                continue
            if self._git.invalid_tag_exists(version, sha):
                msg = f"Existing tag {version} points at the wrong commit (expected {sha})"  # noqa: E501
                error(msg)
                continue
            if self._release_exists(version):
                info(f"Release {version} already exists")
                continue
            valid[version] = sha
        return valid

    def _versions(self, min_age: Optional[timedelta] = None) -> Dict[str, str]:
        """Get all package versions from the registry."""
        kwargs = {}
        if min_age:
            # Get the most recent commit from before min_age.
            until = datetime.now() - min_age
            commits = self._registry.get_commits(until=until)
            # Get the first value like this because the iterator has no `next` method.
            for commit in commits:
                kwargs["ref"] = commit.commit.sha
                break
            else:
                debug("No registry commits were found")
                return {}
        root = self._registry_path
        try:
            contents = self._registry.get_contents(f"{root}/Versions.toml", **kwargs)
        except UnknownObjectException:
            debug("Versions.toml was not found")
            return {}
        versions = toml.loads(contents.decoded_content.decode())
        return {v: versions[v]["git-tree-sha1"] for v in versions}

    def new_versions(self) -> Dict[str, str]:
        """Get all new versions of the package."""
        current = self._versions()
        debug(f"There are {len(current)} total versions")
        old = self._versions(min_age=self._lookback)
        debug(f"There are {len(old)} new versions")
        versions = {k: v for k, v in current.items() if k not in old}
        return self._filter_map_versions(versions)

    def create_dispatch_event(self, payload: Mapping[str, object]) -> None:
        """Create a repository dispatch event."""
        # PyGithub does not yet support this endpoint (#1299).
        resp = requests.post(
            f"https://api.github.com/repos/{self._repo.full_name}/dispatches",
            headers={
                "Accept": "application/vnd.github.everest-preview+json",
                "Authorization": f"token {self._token}",
            },
            json={"event_type": "TagBot", "client_payload": payload},
        )
        debug(f"Dispatch response code: {resp.status_code}")

    def configure_ssh(self, key: str, password: Optional[str]) -> None:
        """Configure the repo to use an SSH key for authentication."""
        self._git.set_remote_url(self._repo.ssh_url)
        _, priv = mkstemp(prefix="tagbot_key_")
        with open(priv, "w") as f:
            # SSH keys must end with a single newline.
            f.write(self._maybe_b64(key).strip() + "\n")
        os.chmod(priv, S_IREAD)
        # Add the host key to a known hosts file
        # so that we don't have to confirm anything when we try to push.
        _, hosts = mkstemp(prefix="tagbot_hosts_")
        with open(hosts, "w") as f:
            subprocess.run(
                ["ssh-keyscan", "-t", "rsa", "github.com"],
                check=True,
                stdout=f,
                stderr=DEVNULL,
            )
        cmd = f"ssh -i {priv} -o UserKnownHostsFile={hosts}"
        debug(f"SSH command: {cmd}")
        self._git.config("core.sshCommand", cmd)
        if password:
            # Start the SSH agent, apply the environment changes,
            # then add our identity so that we don't need to supply a password anymore.
            proc = subprocess.run(
                ["ssh-agent"], check=True, text=True, capture_output=True,
            )
            for (k, v) in re.findall(r"\s*(.+)=(.+?);", proc.stdout):
                debug(f"Setting environment variable {k}={v}")
                os.environ[k] = v
            child = pexpect.spawn(f"ssh-add {priv}")
            child.expect("Enter passphrase")
            child.sendline(password)
            child.expect("Identity added")

    def configure_gpg(self, key: str, password: Optional[str]) -> None:
        """Configure the repo to sign tags with GPG."""
        home = os.environ["GNUPGHOME"] = mkdtemp(prefix="tagbot_gpg_")
        os.chmod(home, S_IREAD | S_IWRITE | S_IEXEC)
        debug(f"Set GNUPGHOME to {home}")
        gpg = GPG(gnupghome=home, use_agent=True)
        # For some reason, this doesn't require the password even though the CLI does.
        import_result = gpg.import_keys(self._maybe_b64(key))
        if import_result.sec_imported != 1:
            warn(import_result.stderr)
            raise Abort("Importing key failed")
        key_id = import_result.fingerprints[0]
        debug(f"GPG key ID: {key_id}")
        if password:
            # Sign some dummy data to put our password into the GPG agent,
            # so that we don't need to supply the password when we create a tag.
            sign_result = gpg.sign("test", passphrase=password)
            if sign_result.status != "signature created":
                warn(sign_result.stderr)
                raise Abort("Testing GPG key failed")
        self._git.config("user.signingKey", key_id)
        self._git.config("user.name", "github-actions[bot]")
        self._git.config("user.email", "actions@github.com")
        self._git.config("tag.gpgSign", "true")

    def handle_release_branch(self, version: str) -> None:
        """Merge an existing release branch or create a PR to merge it."""
        branch = f"release-{version[1:]}"
        if not self._git.fetch_branch(branch):
            info(f"Release branch {branch} does not exist")
            return
        if self._git.can_fast_forward(branch):
            info("Release branch can be fast-forwarded")
            self._git.merge_and_delete_branch(branch)
        else:
            info("Release branch cannot be fast-forwarded, creating pull request")
            self._create_release_branch_pr(version, branch)

    def create_release(self, version: str, sha: str) -> None:
        """Create a GitHub release."""
        target = sha
        if self._git.commit_sha_of_default() == sha:
            # If we use <branch> as the target, GitHub will show
            # "<n> commits to <branch> since this release" on the release page.
            target = self._repo.default_branch
        debug(f"Release {version} target: {target}")
        log = self._changelog.get(version, sha)
        # If we have a custom key, use it to create and push a tag via Git CLI,
        # rather than through the GitHub API.
        if self._ssh or self._gpg:
            info(f"Manually creating tag {version}")
            # The tag only needs to be annotated if we want to sign it.
            self._git.create_tag(version, sha, annotate=self._gpg)
        info(f"Creating release {version} at {sha}")
        self._repo.create_git_release(version, version, log, target_commitish=target)
