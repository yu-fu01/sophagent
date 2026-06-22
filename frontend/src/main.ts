import { createApp } from "vue";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import App from "./App.vue";
import "./styles/tokens.css";
import "./styles/app.css";

createApp(App).use(ElementPlus).mount("#app");
