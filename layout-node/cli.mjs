// Раскладчик bpmn-auto-layout как фильтр: XML на вход, XML с координатами на выход.
// Вызывается из xlsx2bpmn через stdin/stdout, в сеть не ходит.
import { layoutProcess } from 'bpmn-auto-layout';

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    process.stdout.write(await layoutProcess(input));
  } catch (error) {
    console.error(error && error.message ? error.message : String(error));
    process.exit(1);
  }
});
