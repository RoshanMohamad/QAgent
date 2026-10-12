FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -o /inventory-api .

FROM gcr.io/distroless/static-debian12
COPY --from=build /inventory-api /inventory-api
EXPOSE 8084
ENTRYPOINT ["/inventory-api"]
