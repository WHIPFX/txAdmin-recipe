// Reads each recipe as txAdmin does (js-yaml with its JSON schema) and checks it with the same rules txAdmin's
// recipe parser and task validators use (core/deployer/recipeParser.ts and recipeEngine.js), so a recipe that would
// fail on txAdmin's setup page fails here first. CI runs it on every recipe, from GitHub and for the CDN:
//
//     NODE_PATH=<folder with js-yaml 4> node tools/validate_recipes.cjs whip-*.yaml
//
// It also checks each recipe writes the get-whip-media scripts (they add the picture packs) and its server.cfg, and that
// the Legacy and Enhanced recipes download the same files.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const YAML = require('js-yaml');

const isPathLinear = (p) => p.match(/(\.\.(\/|\\|$))+/g) === null;
const isPathRoot = (p) => /^\.[/\\]*$/.test(p);
const isPathValid = (p, acceptRoot = true) =>
    typeof p == 'string' && p.length > 0 && isPathLinear(p) && (acceptRoot || !isPathRoot(p));

// The actions WHIP's recipes use, with txAdmin's own validators for them
const validators = {
    download_github: (t) => typeof t.src == 'string' && isPathValid(t.dest, false)
        && ['string', 'undefined'].includes(typeof t.ref) && ['string', 'undefined'].includes(typeof t.subpath),
    download_file: (t) => typeof t.url == 'string' && isPathValid(t.path) && !t.path.endsWith('/'),
    unzip: (t) => isPathValid(t.src, false) && isPathValid(t.dest),
    remove_path: (t) => isPathValid(t.path, false),
    write_file: (t) => typeof t.data == 'string' && t.data.length > 0 && isPathValid(t.file, false),
    connect_database: () => true,
};

function check(file) {
    const raw = fs.readFileSync(file, 'utf8');
    if (raw.includes('\r')) throw new Error('has CR line endings');
    const recipe = YAML.load(raw, { schema: YAML.JSON_SCHEMA });
    if (typeof recipe !== 'object' || recipe === null) throw new Error("couldn't resolve to an object");
    if (!Array.isArray(recipe.tasks)) throw new Error('no tasks array found');
    if (recipe.$onesync !== 'on') throw new Error('$onesync must be "on"');
    if (recipe.$engine !== 3) throw new Error('$engine must be 3');
    for (const key of ['name', 'version', 'author', 'description']) {
        if (typeof recipe[key] !== 'string' || !recipe[key].trim()) throw new Error(`no ${key}`);
    }
    if (recipe.name.length > 24) throw new Error(`name "${recipe.name}" is over txAdmin's 24 characters`);
    if (recipe.description.length > 256) throw new Error(`description is ${recipe.description.length} characters, over 256`);
    recipe.tasks.forEach((task, i) => {
        if (typeof task.action !== 'string') throw new Error(`[task${i + 1}] no action specified`);
        const valid = validators[task.action];
        if (!valid) throw new Error(`[task${i + 1}] unknown action '${task.action}'`);
        if (!valid(task)) throw new Error(`[task${i + 1}:${task.action}] invalid parameters`);
        if (task.timeoutSeconds !== undefined && !Number.isInteger(task.timeoutSeconds)) {
            throw new Error(`[task${i + 1}] timeoutSeconds must be a number`);
        }
    });
    // Each download_file must go to a file of its own in ./tmp, unzipped by the next task
    const downloads = recipe.tasks.filter((t) => t.action === 'download_file').map((t) => t.path);
    if (new Set(downloads).size !== downloads.length) throw new Error('two downloads write the same file');
    recipe.tasks.forEach((t, i) => {
        if (t.action !== 'download_file') return;
        const next = recipe.tasks[i + 1];
        if (!next || next.action !== 'unzip' || next.src !== t.path) throw new Error(`${t.url} is not unzipped next`);
    });
    const files = recipe.tasks.filter((t) => t.action === 'write_file').map((t) => t.file);
    if (files[files.length - 1] !== './server.cfg') throw new Error('the last write_file is not ./server.cfg');
    return recipe;
}

// What a recipe installs, without the server.cfg and the media scripts. A resource with models for both games has a
// set of zips for each (<repo>-legacy-<n>.zip, <repo>-enhanced-<n>.zip), and each recipe downloads its own game's: those
// names are compared without the game.
const installs = (recipe, game) => JSON.parse(JSON.stringify(recipe.tasks.filter((t) => t.action !== 'write_file'))
    .split(`-${game}-`).join('-GAME-'));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

let failed = false;
const parsed = {};
for (const file of process.argv.slice(2)) {
    try {
        parsed[path.basename(file)] = check(file);
        console.log(`ok    ${file}`);
    } catch (error) {
        failed = true;
        console.log(`FAIL  ${file}: ${error.message}`);
    }
}

for (const [name, recipe] of Object.entries(parsed)) {
    const written = recipe.tasks.filter((t) => t.action === 'write_file').map((t) => t.file).sort();
    if (!same(written, ['./get-whip-media.ps1', './get-whip-media.sh', './server.cfg'])) {
        failed = true;
        console.log(`FAIL  ${name}: writes ${written.join(', ')}`);
    }
}
for (const [name, legacy] of Object.entries(parsed)) {
    if (!name.includes('-legacy')) continue;
    const enhanced = parsed[name.replace('-legacy', '-enhanced')];
    if (enhanced && !same(installs(legacy, 'legacy'), installs(enhanced, 'enhanced'))) {
        failed = true;
        console.log(`FAIL  ${name}: installs something the Enhanced recipe doesn't, or the other way round`);
    }
}
if (failed) process.exit(1);
console.log(`${Object.keys(parsed).length} recipes ok`);
