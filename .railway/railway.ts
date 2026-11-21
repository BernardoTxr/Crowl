import {
  defineRailway,
  github,
  group,
  postgres,
  project,
  service,
} from "railway/iac";

const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

function repositoryName(): string {
  const value =
    process.env.CROWL_RAILWAY_GITHUB_REPO ?? process.env.GITHUB_REPOSITORY;

  if (!value || !REPOSITORY_PATTERN.test(value)) {
    throw new Error(
      "Set CROWL_RAILWAY_GITHUB_REPO to the GitHub owner/repository before planning.",
    );
  }

  return value;
}

export default defineRailway((context) => {
  const repository = repositoryName();
  const branch = process.env.CROWL_RAILWAY_GITHUB_BRANCH ?? "main";
  const projectName =
    process.env.CROWL_RAILWAY_PROJECT_NAME ?? context.projectName ?? "crowl";

  const database = postgres("postgres");

  // The API Dockerfile needs the repository root as its build context because
  // it copies the shared Python package and version-controlled catalog data.
  const api = service("api", {
    source: github(repository, { branch }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "services/api/Dockerfile",
      watchPatterns: [
        "/services/api/**",
        "/packages/crowl-core/**",
        "/data/**",
        "/migrations/**",
      ],
    },
    preDeploy: "alembic -c /app/migrations/alembic.ini upgrade head",
    replicas: 1,
    deploy: {
      healthcheckPath: "/health/ready",
      healthcheckTimeout: 120,
      restartPolicyType: "ALWAYS",
    },
    env: {
      DATABASE_URL: database.env.DATABASE_URL,
      PORT: "8000",
      CROWL_DATA_DIR: "/app/data",
      CROWL_AUTO_CREATE_SCHEMA: "false",
      CROWL_AUTO_SEED: "true",
      CROWL_SEED_FIXTURES: "false",
      CROWL_FRESHNESS_HOURS: "36",
      CROWL_DURATION_HISTORY_DAYS: "180",
      CROWL_CORS_ORIGINS: "",
      CROWL_SCHEMA_REVISION: "0006_flixbus_fare_policy",
    },
  });

  const web = service("web", {
    source: github(repository, {
      branch,
      rootDirectory: "services/web",
    }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "Dockerfile",
      watchPatterns: ["/services/web/**"],
    },
    replicas: 1,
    deploy: {
      healthcheckPath: "/healthz",
      healthcheckTimeout: 60,
      restartPolicyType: "ALWAYS",
    },
    env: {
      PORT: "80",
      API_URL: "http://api.railway.internal:8000",
    },
  });

  const crawler = service("crawler", {
    source: github(repository, {
      branch,
    }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "services/crawler/Dockerfile",
      watchPatterns: [
        "/services/crawler/**",
        "/packages/crowl-core/**",
        "/data/**",
      ],
    },
    start: "crowl-crawler run",
    deploy: {
      cronSchedule: "0 2 * * *",
      restartPolicyType: "NEVER",
    },
    env: {
      DATABASE_URL: database.env.DATABASE_URL,
      CROWL_DATA_DIR: "/app/data",
      CROWL_BATCH_DEADLINE_HOURS: "20",
      CROWL_STRICT_SCHENGEN_PATH: "true",
      CROWL_LOG_LEVEL: "INFO",
      CROWL_CRAWLER_RELEASE: "production-v1",
      CROWL_FLIXBUS_PUBLIC_SEARCH_URL_TEMPLATE:
        "https://shop.global.flixbus.com/search?departureCity={origin}&arrivalCity={destination}&rideDate={date_dot}&adult=1&currency=EUR",
      CROWL_FLIXBUS_TERMINAL_ALIASES_JSON:
        '{"par-bercy-seine-bus":"40de8964-8646-11e6-9066-549f350fcb0c","ams-sloterdijk-bus":"40dde3b8-8646-11e6-9066-549f350fcb0c","bru-gare-du-midi-bus":"40de6287-8646-11e6-9066-549f350fcb0c"}',
      CROWL_FLIXBUS_STOP_ALIASES_JSON:
        '{"Paris (Bercy Seine)":"par-bercy-seine-bus","Amsterdam Sloterdijk":"ams-sloterdijk-bus","Brussels South (Gare du Midi)":"bru-gare-du-midi-bus"}',
      CROWL_FLIXBUS_COVERAGE_JSON:
        '{"supported_terminal_pairs":[["par-bercy-seine-bus","ams-sloterdijk-bus"],["par-bercy-seine-bus","bru-gare-du-midi-bus"]],"sellable_through":"2027-05-24"}',
    },
  });

  const backend = group("Backend", [database, api, crawler]);

  return project(projectName, {
    resources: [web, backend],
  });
});
