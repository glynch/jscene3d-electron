#!/usr/bin/env node

const fs = require('node:fs');

const replacementArgument = process.argv.find((argument) => argument.startsWith('--replacement='));
const replacement = replacementArgument?.slice('--replacement='.length) ?? '320x240';
const [replacementWidth, replacementHeight] = replacement.split('x');
const exitAfterReady = process.argv.includes('--exit-after-ready');
const ignoreShutdown = process.argv.includes('--ignore-shutdown');
const shutdownMarkerArgument = process.argv.find((argument) => argument.startsWith('--shutdown-marker='));
const shutdownMarker = shutdownMarkerArgument?.slice('--shutdown-marker='.length);

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
    } else if (command === 'SHUTDOWN' && !ignoreShutdown) {
      process.stdout.write('SHUTDOWN_READY\n', (error) => {
        if (error) {
          process.exit(20);
        }
        if (shutdownMarker) {
          fs.writeFileSync(shutdownMarker, command);
        }
        process.exit(0);
      });
    } else if (command === 'QUIT' && !ignoreShutdown) {
      process.exit(0);
    }
  }
});
