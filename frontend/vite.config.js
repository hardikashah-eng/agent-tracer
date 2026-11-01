import { resolve } from 'path'
import { defineConfig } from 'vite';
export default defineConfig({build:{rollupOptions:{input:{main:resolve(__dirname,'index.html'),sidecar:resolve(__dirname,'sidecar.html')}}},server:{proxy:{'/api':'http://localhost:8000','/ws':{target:'ws://localhost:8000',ws:true}}}});
