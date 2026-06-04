import { join } from "path";
const helper = require("fs");

export function bootstrap(): void {
  execute();
  join("a", "b");
}

export class Router {
  run() {
    bootstrap();
  }
}

const execute = () => {
  helper.readFileSync("x");
};
