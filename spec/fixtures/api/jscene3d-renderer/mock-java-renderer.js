#!/usr/bin/env node

const replacementArgument = process.argv.find((argument) => argument.startsWith('--replacement='));
const replacement = replacementArgument?.slice('--replacement='.length) ?? '320x240';
const [replacementWidth, replacementHeight] = replacement.split('x');
const exitAfterReady = process.argv.includes('--exit-after-ready');
const ignoreQuit = process.argv.includes('--ignore-quit');

process.stdout.write('RENDERER_READY\n');

if (exitAfterReady) {
  setImmediate(() => process.exit(19));
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  input += chunk;
  while (input.includes('\n')) {
    const newline = input.indexOf('\n');
    const command = input.slice(0, newline).trim();
    input = input.slice(newline + 1);
    if (command === 'RECEIVE_SURFACE') {
      process.stdout.write(`SURFACE_READY ${replacementWidth} ${replacementHeight}\n`);
    } else if (command.startsWith('FRAME')) {
      process.stdout.write('FRAME_READY\n');
    } else if (command === 'QUIT' && !ignoreQuit) {
      process.exit(0);
    }
  }
});
