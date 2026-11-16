// Posts a WHIP resource's release to Discord, with its changelog. GitHub runs it (.github/workflows/discord-release.yml)
// when a resource publishes a release: each resource repo has templates/discord-release.yml as
// .github/workflows/discord-release.yml. Discord's own GitHub webhook shows only "New release published".
//
// The changelog, first that applies:
//   1. A "## Changelog" section the release's notes have, written by hand: that, as it is.
//   2. GitHub's generated notes ("What's Changed"): each PR, by kind (feat New, fix Fixed, perf Faster, the rest
//      Changed), with who made it and the first paragraph of its description.
//   3. Notes that are only "Full Changelog: <compare>": the commits between the two tags, by kind.
//   4. Notes written by hand without that heading: those, as they are.
//
// It also runs on a computer (Node 18 or newer), printing the message instead of posting it:
//     GH_TOKEN=$(gh auth token) node tools/discord_release.mjs WHIPFX/whip_core v1.2.0 --dry-run
// With DISCORD_WEBHOOK set and no --dry-run, it posts.

const [repo, tag, flag] = process.argv.slice(2);
const dryRun = flag === '--dry-run' || !process.env.DISCORD_WEBHOOK;
if (!repo || !tag) { console.error('usage: discord_release.mjs <owner/repo> <tag> [--dry-run]'); process.exit(2); }

const KINDS = [['feat', 'New'], ['fix', 'Fixed'], ['perf', 'Faster'], ['', 'Changed']];
const SUMMARY = 220;      // characters of a PR's description
const MAX_COMMITS = 25;   // commits listed before "and N more"
const MAX_TEXT = 3900;    // Discord takes 4096 in an embed's description

const gh = async (path) => {
    const r = await fetch(`https://api.github.com/${path}`, { headers: {
        Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'whip-discord-release',
        ...(process.env.GH_TOKEN ? { Authorization: `Bearer ${process.env.GH_TOKEN}` } : {}),
    } });
    if (!r.ok) throw new Error(`GitHub ${path}: ${r.status}`);
    return r.json();
};

/** A title's kind and the title without its prefix, capitalised: 'feat: x' -> ['New', 'X']. */
function kindOf(title) {
    // Only the conventional types count as a prefix ('Fix/' as well as 'fix:'), so 'OpenShop: ...' keeps its word.
    const m = title.match(/^(feat|fix|perf|chore|docs|refactor|ci|style|test|build|revert)(\([^)]*\))?!?[:/]\s*(.*)$/i);
    const kind = m ? m[1].toLowerCase() : '';
    const label = (KINDS.find(([k]) => k === kind) || KINDS[KINDS.length - 1])[1];
    const text = (m ? m[3] : title).trim();
    return [label, text.charAt(0).toUpperCase() + text.slice(1)];
}

/** The first paragraph of a PR's description, as plain text, cut at a word near SUMMARY. */
function summaryOf(body) {
    const para = (body || '').replace(/\r/g, '').split(/\n\s*\n/).map((p) => p.trim())
        .find((p) => p && !/^(#|<!--|- \[|```|\|)/.test(p)) || '';
    let text = para.replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
        // Bold and code marks go; names like whip_core and refs like #22 stay as they are.
        .replace(/^\s*[-*]\s+/gm, '').replace(/^\s*(>|#+)\s*/gm, '').replace(/\*\*|`/g, '').replace(/\s+/g, ' ').trim();
    if (text.length > SUMMARY) text = text.slice(0, text.lastIndexOf(' ', SUMMARY)).replace(/[,;:.]$/, '') + '…';
    return text;
}

/** Hand-written markdown as Discord shows it in an embed: headings as bold lines, '* ' lists as bullets. */
function handWritten(md) {
    return md.replace(/\r/g, '').replace(/^\*\*Full Changelog\*\*:.*$/gm, '')
        .replace(/^#{1,6}\s+(.+)$/gm, '**$1**').replace(/^\s*\*\s+/gm, '• ')
        .replace(/\n{3,}/g, '\n\n').trim();
}

function grouped(entries) {
    const groups = new Map(KINDS.map(([, label]) => [label, []]));
    for (const [label, line] of entries) groups.get(label).push(line);
    const parts = [];
    // (A PR with its summary takes two lines: a blank one between them. A commit is one line.)
    for (const [label, lines] of groups) {
        if (lines.length) parts.push(`**${label}**\n${lines.join(lines.some((l) => l.includes('\n')) ? '\n\n' : '\n')}`);
    }
    return parts.join('\n\n');
}

async function fromPulls(numbers, body) {
    const prs = await Promise.all(numbers.map((n) => gh(`repos/${repo}/pulls/${n}`)));
    const entries = prs.map((pr) => {
        // A title GitHub cut short ends with '…', and its description starts with the rest of it.
        let title = pr.title, text = pr.body || '';
        if (title.endsWith('…') && text.startsWith('…')) {
            const rest = text.slice(1).split(/\r?\n/)[0];
            title = title.slice(0, -1) + rest;
            text = text.slice(1 + rest.length);
        }
        const [label, name] = kindOf(title.replace(/\s*\(\d+\.\d+\.\d+\)\s*$/, ''));
        const summary = summaryOf(text);
        return [label, `• **${name}** ([#${pr.number}](${pr.html_url})) by ${pr.user.login}` + (summary ? `\n${summary}` : '')];
    });
    let out = grouped(entries);
    const fresh = [...body.matchAll(/^\* @(\S+) made their first contribution/gm)].map((m) => m[1]);
    if (fresh.length) out += `\n\nWelcome to our new contributor${fresh.length > 1 ? 's' : ''}: ${fresh.join(', ')}`;
    return { text: out, count: prs.length };
}

async function fromCommits(base) {
    const cmp = await gh(`repos/${repo}/compare/${encodeURIComponent(base)}...${encodeURIComponent(tag)}`);
    const commits = cmp.commits.filter((c) => c.parents.length < 2)
        .map((c) => ({ sha: c.sha, url: c.html_url, subject: c.commit.message.split('\n')[0] }))
        .filter((c) => !/^chore: version \S+$/i.test(c.subject));
    const shown = commits.slice(-MAX_COMMITS).reverse();
    const entries = shown.map((c) => {
        const [label, name] = kindOf(c.subject);
        return [label, `• ${name} ([${c.sha.slice(0, 7)}](${c.url}))`];
    });
    let out = grouped(entries);
    if (commits.length > shown.length) out += `\n\n…and ${commits.length - shown.length} earlier change${commits.length - shown.length > 1 ? 's' : ''}`;
    return { text: out, count: commits.length };
}

async function changelog(rel) {
    const body = (rel.body || '').replace(/\r/g, '');
    const own = body.match(/^##\s*Changelog\s*\n([\s\S]*?)(?=^##\s|(?![\s\S]))/m);
    if (own && own[1].trim()) return { text: handWritten(own[1]), count: null };

    const pulls = [...new Set(body.split('\n')
        .filter((l) => l.startsWith('* ') && !/ made their first contribution /.test(l))
        .map((l) => (l.match(/\/pull\/(\d+)\s*$/) || [])[1]).filter(Boolean).map(Number))];
    if (pulls.length) return fromPulls(pulls, body);

    const rest = body.replace(/^\*\*Full Changelog\*\*:.*$/m, '').trim();
    const cmp = body.match(/\*\*Full Changelog\*\*: \S+\/compare\/(\S+?)\.\.\.\S+/);
    if (!rest && cmp) return fromCommits(cmp[1]);
    if (!rest) return { text: 'The first release.', count: null };
    return { text: handWritten(rest), count: null };
}

async function main() {
    const rel = await gh(`repos/${repo}/releases/tags/${encodeURIComponent(tag)}`);
    let { text, count } = await changelog(rel);
    if (text.length > MAX_TEXT) text = text.slice(0, text.lastIndexOf('\n', MAX_TEXT)) + '\n…and more in the full notes.';
    const name = repo.split('/')[1];
    const payload = {
        allowed_mentions: { parse: [] },   // a name in the notes never pings anyone
        embeds: [{
            title: `${name} ${rel.tag_name}`.slice(0, 256),
            url: rel.html_url,
            description: text,
            color: 0x5865f2,
            footer: { text: `${count ? `${count} change${count === 1 ? '' : 's'} · ` : ''}full notes and the download on GitHub` },
            timestamp: rel.published_at || new Date().toISOString(),
        }],
    };
    if (dryRun) {
        console.log(`${payload.embeds[0].title}\n\n${text}\n\n— ${payload.embeds[0].footer.text}`);
        return;
    }
    for (let attempt = 1; attempt <= 3; attempt++) {
        const r = await fetch(process.env.DISCORD_WEBHOOK, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        if (r.ok) { console.log(`Posted ${payload.embeds[0].title}`); return; }
        if (r.status === 429 && attempt < 3) {
            const wait = Number((await r.json().catch(() => ({}))).retry_after) || 2;
            await new Promise((res) => setTimeout(res, Math.ceil(wait * 1000)));
            continue;
        }
        throw new Error(`Discord answered ${r.status}: ${(await r.text()).slice(0, 300)}`);
    }
}
main().catch((e) => { console.error(e.message); process.exit(1); });
