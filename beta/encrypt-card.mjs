// Card input is accepted through stdin only. Never print input or crypto errors.
import { constants, createPublicKey, publicEncrypt } from 'node:crypto';

let input = '';
try {
  for await (const chunk of process.stdin) {
    input += chunk;
    if (input.length > 65536) throw new Error();
  }
  const data = JSON.parse(input);
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(data.publicKeyDER ?? '') ||
      typeof data.publicKeyHash !== 'string' || !data.publicKeyHash.length ||
      data.publicKeyHash.length > 1024 ||
      !/^\d{12,19}$/.test(data.number ?? '') ||
      !/^\d{3,4}$/.test(data.securityCode ?? '')) throw new Error();
  const key = createPublicKey({ key: Buffer.from(data.publicKeyDER, 'base64'),
    format: 'der', type: 'spki' });
  const bits = key.asymmetricKeyDetails?.modulusLength;
  if (key.asymmetricKeyType !== 'rsa' || bits < 2048 || bits > 8192) throw new Error();
  const encrypt = value => JSON.stringify({ cipherText: publicEncrypt({ key,
    padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256',
    oaepLabel: Buffer.alloc(0) }, Buffer.from(value, 'utf8')).toString('base64'),
    publicKeyHash: data.publicKeyHash });
  process.stdout.write(JSON.stringify({ cardNumber: encrypt(data.number),
    securityCode: encrypt(data.securityCode) }));
  input = '';
} catch {
  input = '';
  process.stderr.write('Card encryption failed.');
  process.exitCode = 2;
}
