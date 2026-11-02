import { ProxyAgent, setGlobalDispatcher } from 'undici';

const proxy = process.env.https_proxy || process.env.HTTPS_PROXY || process.env.http_proxy || process.env.HTTP_PROXY;
if (!proxy) throw new Error('dsh-access-policy: SRT proxy environment is missing');
// Always use the policy proxy, including explicitly allowed private destinations.
setGlobalDispatcher(new ProxyAgent({ uri: proxy }));
